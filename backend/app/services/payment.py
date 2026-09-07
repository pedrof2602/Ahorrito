"""Cuánto sale la canasta según con qué pagues.

El cálculo (`evaluate_option`, `rank_options`) es puro: recibe totales ya
resueltos y devuelve el desglose, sin I/O ni reloj, para poder testear
exhaustivamente los casos borde. `best_payment` es la parte que habla con la red
y con la base, igual que la separación que ya tiene `services/basket.py`.

Hay tres formas de que este cálculo dé un número convincente y equivocado, y el
módulo existe para evitar las tres:

1. **Calcular el descuento del super en vez de preguntarlo.** Las promos de VTEX
   no se acumulan —el motor elige una por ítem—, así que multiplicar el total por
   el porcentaje del teaser exagera el ahorro. Medido sobre una canasta de tres
   productos: ahorro real de "Tarjeta Carrefour 15%" $1.499, cuenta ingenua
   $3.991. Por eso `provider_saving` sale de restar dos simulaciones, nunca de
   una multiplicación.
2. **Sumar el descuento del banco al del super.** Los supers declaran que no son
   acumulables. Se suma solo si la regla dice explícitamente que sí.
3. **Ignorar el tope.** Un 35% con tope de $6.000 deja de ser 35% a partir de los
   $17.143 de compra. Sin esto, cuanto más grande la canasta, más miente.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence
from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories import PromoUsageRepository
from app.models.basket import BasketComparison, BasketCompareRequest
from app.models.catalog import BasketSimulation, Store
from app.models.payment import (
    AppliedBankPromo,
    BenefitType,
    BestPaymentRequest,
    BestPaymentResult,
    BinVerification,
    BinVerificationResult,
    CapPeriod,
    ChainPaymentSummary,
    PaymentConfidence,
    PaymentInstrument,
    PaymentOption,
    PaymentPromoRule,
    period_start_for,
)
from app.services.basket import compare_basket
from app.services.promo_catalog import get_catalog
from app.services.providers.base import PriceProvider
from app.services.providers.registry import ProviderRegistry

logger = logging.getLogger(__name__)

_CAP_PERIOD_LABEL = {
    CapPeriod.PER_TRANSACTION: "por compra",
    CapPeriod.DAILY: "por día",
    CapPeriod.WEEKLY: "semanal",
    CapPeriod.BIWEEKLY: "quincenal",
    CapPeriod.MONTHLY: "mensual",
}


def _bank_saving(
    rule: PaymentPromoRule, subtotal_cents: int, used_cents: int
) -> tuple[int, int, int | None, bool]:
    """`(ahorro, ahorro_sin_tope, tope_restante, topeado)`.

    El tope acota **el ahorro**, no el monto de la compra. Confundirlo —aplicar
    el porcentaje solo hasta que la compra llegue al tope— es el error clásico y
    da un número bastante más grande.
    """
    uncapped = round(subtotal_cents * rule.percent / 100)
    if rule.cap_cents is None:
        return uncapped, uncapped, None, False
    remaining = max(rule.cap_cents - used_cents, 0)
    return min(uncapped, remaining), uncapped, remaining, uncapped > remaining


def _pick_bank_promo(
    rules: Sequence[PaymentPromoRule],
    *,
    subtotal_cents: int,
    provider_saving_cents: int,
    cap_used_cents: Mapping[str, int],
) -> AppliedBankPromo | None:
    """La regla que más ahorra entre las que realmente pueden aplicarse.

    Se evalúa sobre el subtotal **ya descontado** por la cadena: el banco
    reintegra sobre lo que pagaste, no sobre el precio de lista.
    """
    best: AppliedBankPromo | None = None

    for rule in rules:
        # Si la cadena ya aplicó una promo, la bancaria solo entra cuando la
        # regla declara que acumula. Es lo que publican los supers, y suponer lo
        # contrario infla el ahorro justo en los casos más atractivos.
        if provider_saving_cents > 0 and not rule.stacks_with_provider_promos:
            continue
        if (
            rule.minimum_purchase_cents is not None
            and subtotal_cents < rule.minimum_purchase_cents
        ):
            continue

        saving, uncapped, remaining, capped = _bank_saving(
            rule, subtotal_cents, cap_used_cents.get(rule.id, 0)
        )
        if saving <= 0:
            # Tope agotado. No es un beneficio, y mostrarlo con ahorro cero solo
            # ensucia el ranking.
            continue

        candidate = AppliedBankPromo(
            rule_id=rule.id,
            name=rule.name,
            percent=rule.percent,
            benefit_type=rule.benefit_type,
            saving_cents=saving,
            uncapped_saving_cents=uncapped,
            cap_cents=rule.cap_cents,
            cap_remaining_cents=remaining,
            cap_period=rule.cap_period,
            capped=capped,
            unverified=rule.unverified,
        )
        if best is None or candidate.saving_cents > best.saving_cents:
            best = candidate

    return best


def _caveats(
    instrument: PaymentInstrument,
    simulation: BasketSimulation | None,
    bank_promo: AppliedBankPromo | None,
) -> list[str]:
    notes: list[str] = []

    if simulation is None:
        notes.append(
            "La cadena no tiene checkout consultable: el total sale del catálogo "
            "y no incluye promos que se apliquen recién en la caja."
        )
    elif not simulation.complete:
        notes.append(
            f"El checkout no confirmó {len(simulation.missing_sku_ids)} producto(s): "
            "el total está incompleto."
        )
    if not instrument.simulatable:
        notes.append(
            "Sin BIN no se le puede preguntar el precio real a la cadena; solo "
            "se aplican reglas cargadas a mano."
        )
    if bank_promo is not None:
        if bank_promo.unverified:
            notes.append(
                f"«{bank_promo.name}» no está verificada contra la fuente: el "
                "ahorro es una estimación."
            )
        if bank_promo.capped:
            period = _CAP_PERIOD_LABEL[bank_promo.cap_period]
            notes.append(
                f"Llegaste al tope {period} de «{bank_promo.name}»: agregar más "
                "productos ya no aumenta el descuento."
            )
        if bank_promo.benefit_type is BenefitType.REINTEGRO:
            notes.append(
                "Es un reintegro: en la caja pagás el total sin descontar y te lo "
                "devuelven después."
            )
    return notes


def evaluate_option(
    *,
    chain_slug: str,
    chain_display_name: str,
    instrument: PaymentInstrument,
    base_total_cents: int,
    rules: Sequence[PaymentPromoRule] = (),
    simulation: BasketSimulation | None = None,
    baseline_simulation: BasketSimulation | None = None,
    cap_used_cents: Mapping[str, int] | None = None,
) -> PaymentOption:
    """Evalúa una combinación de cadena y medio de pago.

    - `base_total_cents`: total de catálogo, el que ya devuelve `/basket/compare`.
    - `baseline_simulation`: la canasta simulada **sin** tarjeta.
    - `simulation`: la misma canasta simulada **con** el BIN de `instrument`.

    Las dos simulaciones son necesarias para aislar lo que aporta la tarjeta: el
    checkout también aplica promos por cantidad que el catálogo no refleja, así
    que restarle el total con tarjeta al total de catálogo le atribuiría a la
    tarjeta descuentos que no son suyos.
    """
    cap_used_cents = cap_used_cents or {}

    # El piso honesto: lo que pagarías sin ningún beneficio de medio de pago,
    # confirmado por el checkout cuando se pudo.
    reference_total = (
        baseline_simulation.total_cents
        if baseline_simulation is not None and baseline_simulation.complete
        else base_total_cents
    )

    if simulation is not None and simulation.complete:
        provider_total = simulation.total_cents
        provider_promotions = simulation.promotions
    else:
        provider_total = reference_total
        provider_promotions = ()

    provider_saving = max(reference_total - provider_total, 0)

    bank_promo = _pick_bank_promo(
        rules,
        subtotal_cents=provider_total,
        provider_saving_cents=provider_saving,
        cap_used_cents=cap_used_cents,
    )
    bank_saving = bank_promo.saving_cents if bank_promo else 0

    final_total = max(provider_total - bank_saving, 0)
    out_of_pocket = (
        provider_total
        if bank_promo is not None and bank_promo.benefit_type is BenefitType.REINTEGRO
        else final_total
    )

    measured = simulation is not None and simulation.complete
    if bank_promo is None:
        # Sin promo bancaria, el número es tan bueno como la fuente del total:
        # medido si el checkout lo confirmó, estimado si salió del catálogo.
        confidence = (
            PaymentConfidence.MEASURED if measured else PaymentConfidence.ESTIMATED
        )
    elif measured and provider_saving > 0:
        confidence = PaymentConfidence.MIXED
    else:
        confidence = PaymentConfidence.ESTIMATED

    return PaymentOption(
        chain_slug=chain_slug,
        chain_display_name=chain_display_name,
        instrument=instrument,
        reference_total_cents=reference_total,
        provider_total_cents=provider_total,
        provider_saving_cents=provider_saving,
        provider_promotions=provider_promotions,
        bank_promo=bank_promo,
        final_total_cents=final_total,
        out_of_pocket_today_cents=out_of_pocket,
        total_saving_cents=max(reference_total - final_total, 0),
        confidence=confidence,
        caveats=_caveats(instrument, simulation, bank_promo),
    )


def rank_options(
    options: Sequence[PaymentOption],
    unresolved: Mapping[str, int] | None = None,
) -> list[PaymentOption]:
    """Ordena por lo que terminás pagando.

    Antes que el precio pesa **cuántas líneas de la lista resuelve la cadena**
    (`unresolved`): una cadena a la que le faltan productos no encabeza aunque
    su total sea el más bajo, porque ese total es más bajo justamente por lo que
    no te vende. Lo que falta lo vas a pagar igual en otro lado, así que
    recomendarla sería recomendar una compra que no cierra.

    Después desempata prefiriendo lo medido sobre lo estimado: entre dos totales
    iguales, el que confirmó el checkout vale más que el que calculamos
    nosotros. Y después por menor desembolso hoy, porque a igual ahorro conviene
    el descuento en el acto antes que el reintegro.
    """
    order = {
        PaymentConfidence.MEASURED: 0,
        PaymentConfidence.MIXED: 1,
        PaymentConfidence.ESTIMATED: 2,
    }
    gaps = unresolved or {}
    return sorted(
        options,
        key=lambda o: (
            gaps.get(o.chain_slug, 0),
            o.final_total_cents,
            order[o.confidence],
            o.out_of_pocket_today_cents,
        ),
    )


# --------------------------------------------------------------- orquestación


def basket_quantities(
    comparison: BasketComparison, chain_slug: str
) -> list[tuple[str, int]]:
    """Los SKU que esa cadena aportó a la canasta, con su cantidad.

    Se agrupa por SKU porque dos líneas distintas ("leche 1L" y "leche entera")
    pueden resolver al mismo producto, y el checkout rechaza el mismo id repetido.
    """
    quantities: dict[str, int] = {}
    for line in comparison.lines:
        for match in line.matches:
            if match.chain_slug == chain_slug:
                sku = match.product.sku_id
                quantities[sku] = quantities.get(sku, 0) + line.quantity
    return list(quantities.items())


async def _cap_usage(
    session: AsyncSession,
    rules: Sequence[PaymentPromoRule],
    on_date: date,
    overrides: Mapping[str, int] | None,
    user_id: int,
) -> dict[str, int]:
    """Consumo de cada tope en su ventana vigente.

    La ventana se calcula por regla: un tope quincenal y uno mensual no se
    reinician el mismo día, y usar una sola ventana para todos haría que uno de
    los dos arrastre consumo que ya venció.
    """
    if overrides is not None:
        return dict(overrides)
    repo = PromoUsageRepository(session, user_id)
    return await repo.usage_map(
        [(rule.id, period_start_for(rule.cap_period, on_date)) for rule in rules]
    )


async def _simulate_for_instruments(
    provider: PriceProvider,
    sku_quantities: Sequence[tuple[str, int]],
    instruments: Sequence[PaymentInstrument],
    *,
    store: Store | None,
    sales_channel: int | None,
) -> tuple[BasketSimulation | None, dict[int, BasketSimulation | None]]:
    """Simula la canasta sin tarjeta y con cada una, en paralelo.

    El baseline no es opcional: sin él no se puede saber qué parte del descuento
    aporta la tarjeta y cuál ya estaba por promos de cantidad.
    """
    simulatable = [i for i in instruments if i.simulatable]

    async def run(card_bin: str | None) -> BasketSimulation | None:
        try:
            return await provider.simulate_basket(
                sku_quantities,
                store=store,
                sales_channel=sales_channel,
                card_bin=card_bin,
            )
        except Exception as exc:  # noqa: BLE001 - degradar al catálogo, no romper
            logger.warning(
                "%s: falló la simulación con bin=%s: %s",
                provider.chain.slug,
                card_bin,
                exc,
            )
            return None

    results = await asyncio.gather(
        run(None), *(run(i.bins[0]) for i in simulatable)
    )
    baseline, per_card = results[0], results[1:]
    by_instrument = {
        # `id` puede ser None si el instrumento no viene de la base; en ese caso
        # la clave es la posición, que igual es única dentro de esta llamada.
        id(instrument): simulation
        for instrument, simulation in zip(simulatable, per_card, strict=True)
    }
    return baseline, by_instrument


PROBE_TERM = "leche"
"""Término con el que se busca un producto para probar un BIN.

Se busca en vez de hardcodear un SKU: un id fijo se da de baja del catálogo y la
verificación empieza a fallar por un motivo que no tiene nada que ver con la
tarjeta."""


async def verify_bin(
    registry: ProviderRegistry, card_bin: str
) -> BinVerificationResult:
    """Prueba un BIN contra el checkout real y reporta qué promos desbloquea.

    Es lo que convierte un BIN declarado en uno medido. La prueba es una compra
    de un solo producto simulada dos veces, con y sin la tarjeta: la diferencia
    es, literalmente, lo que esa tarjeta hace.
    """
    checks: list[BinVerification] = []
    skipped: list[str] = []

    for provider in registry.all():
        slug = provider.chain.slug
        try:
            found = await provider.search(PROBE_TERM, limit=10)
        except Exception as exc:  # noqa: BLE001 - una cadena caída no invalida el resto
            logger.warning("%s: no se pudo buscar el producto de prueba: %s", slug, exc)
            skipped.append(slug)
            continue

        probe = next((o for o in found if o.offer.available), None)
        if probe is None:
            skipped.append(slug)
            continue

        items = [(probe.product.sku_id, 1)]
        without, with_card = await asyncio.gather(
            provider.simulate_basket(items),
            provider.simulate_basket(items, card_bin=card_bin),
        )
        if without is None or with_card is None:
            # Disco cae acá: no tiene checkout consultable, así que no hay nada
            # que medir. No es un fallo de la tarjeta.
            skipped.append(slug)
            continue

        before = {p.name for p in without.promotions}
        checks.append(
            BinVerification(
                chain_slug=slug,
                card_bin=card_bin,
                probe_sku_id=probe.product.sku_id,
                probe_product_name=probe.product.name,
                price_without_card_cents=without.total_cents,
                price_with_card_cents=with_card.total_cents,
                saving_cents=max(without.total_cents - with_card.total_cents, 0),
                promotions_unlocked=sorted(
                    p.name for p in with_card.promotions if p.name not in before
                ),
            )
        )

    unlocked = [c for c in checks if c.unlocked_anything]
    if unlocked:
        names = ", ".join(
            f"{c.chain_slug}: {', '.join(c.promotions_unlocked) or 'descuento sin nombre'}"
            for c in unlocked
        )
        message = f"El BIN activó promociones — {names}."
    elif checks:
        message = (
            "El BIN no activó ninguna promoción hoy. Puede ser una tarjeta sin "
            "beneficios en estas cadenas, o que la promo no corra hoy; no "
            "significa que esté mal cargado."
        )
    else:
        message = (
            "No se pudo comprobar: ninguna cadena tiene checkout consultable "
            "para probarlo."
        )

    return BinVerificationResult(
        card_bin=card_bin,
        verified=bool(unlocked),
        checks=checks,
        skipped_chains=skipped,
        message=message,
    )


async def best_payment(
    registry: ProviderRegistry,
    session: AsyncSession,
    request: BestPaymentRequest,
    instruments: Sequence[PaymentInstrument],
    *,
    user_id: int,
    today: date | None = None,
) -> BestPaymentResult:
    """Ranking de cadena × medio de pago para la canasta pedida.

    Primero resuelve la canasta con el comparador de siempre y después le
    pregunta el precio a cada checkout una vez por tarjeta. La simulación se
    corre **hoy**, aunque `on_date` mire a futuro: el checkout no acepta
    consultas a otro día, así que un cálculo para el sábado que viene mezcla
    promos de cadena de hoy con promos bancarias de ese sábado, y la respuesta lo
    dice en `notes` en vez de disimularlo.
    """
    on_date = request.on_date or today or date.today()
    is_future = on_date != (today or date.today())

    comparison = await compare_basket(
        registry,
        session,
        BasketCompareRequest(
            lines=request.lines,
            postal_code=request.postal_code,
            sales_channel=request.sales_channel,
            chain_slugs=request.chain_slugs,
            verify=False,
            fresh=request.fresh,
        ),
    )

    catalog = get_catalog()
    stores = {c.chain_slug: c.store for c in comparison.chains if c.store}
    options: list[PaymentOption] = []
    summaries: list[ChainPaymentSummary] = []
    used_rules: set[str] = set()

    for chain_total in comparison.chains:
        provider = registry.get(chain_total.chain_slug)
        if provider is None:
            continue
        quantities = basket_quantities(comparison, chain_total.chain_slug)
        if not quantities:
            continue

        baseline, by_instrument = await _simulate_for_instruments(
            provider,
            quantities,
            instruments,
            store=stores.get(chain_total.chain_slug),
            sales_channel=request.sales_channel,
        )

        chain_options: list[PaymentOption] = []
        for instrument in instruments:
            rules = catalog.rules_for(
                instrument,
                chain_slug=chain_total.chain_slug,
                channel=request.channel,
                on_date=on_date,
            )
            used_rules.update(rule.id for rule in rules)
            chain_options.append(
                evaluate_option(
                    chain_slug=chain_total.chain_slug,
                    chain_display_name=chain_total.display_name,
                    instrument=instrument,
                    base_total_cents=chain_total.total_cents,
                    rules=rules,
                    simulation=by_instrument.get(id(instrument)),
                    baseline_simulation=baseline,
                    cap_used_cents=await _cap_usage(
                        session, rules, on_date, request.cap_used_cents, user_id
                    ),
                )
            )

        options.extend(chain_options)
        best = min(chain_options, key=lambda o: o.final_total_cents, default=None)
        summaries.append(
            ChainPaymentSummary(
                chain_slug=chain_total.chain_slug,
                display_name=chain_total.display_name,
                base_total_cents=chain_total.total_cents,
                best_final_total_cents=(
                    best.final_total_cents if best else chain_total.total_cents
                ),
                best_instrument_label=best.instrument.label if best else None,
                simulated=baseline is not None,
            )
        )

    # Cuántas líneas de la lista no resuelve cada cadena (no las tiene, o las
    # tiene sin stock). Manda sobre el precio en los dos rankings.
    unresolved = {
        total.chain_slug: len(total.unresolved_lines) for total in comparison.chains
    }

    ranked = rank_options(options, unresolved)
    notes: list[str] = []
    if incompletas := sorted(slug for slug, gaps in unresolved.items() if gaps):
        notes.append(
            "Estas cadenas no resuelven la lista entera y por eso no encabezan "
            "el ranking, aunque su total sea menor: "
            + ", ".join(
                f"{slug} (le faltan {unresolved[slug]})" for slug in incompletas
            )
            + "."
        )
    if is_future:
        notes.append(
            f"Las promos bancarias se evaluaron para el {on_date.isoformat()}, "
            "pero las de la cadena se simularon hoy: el checkout no acepta "
            "consultas a futuro y sus promos pueden cambiar."
        )
    if comparison.partial:
        notes.append(
            "Alguna cadena falló y su total está incompleto; el ranking puede "
            "no reflejar el precio real."
        )
    if not instruments:
        notes.append(
            "No hay medios de pago cargados: los totales son los de góndola."
        )

    return BestPaymentResult(
        options=ranked,
        chains=sorted(
            summaries,
            key=lambda s: (unresolved.get(s.chain_slug, 0), s.best_final_total_cents),
        ),
        on_date=on_date,
        channel=request.channel,
        winner=ranked[0] if ranked else None,
        baseline_total_cents=(
            min((s.base_total_cents for s in summaries), default=None)
        ),
        unverified_rules=sorted(
            rule.id
            for rule in catalog.rules
            if rule.id in used_rules and rule.unverified
        ),
        notes=notes,
        basket=comparison,
    )
