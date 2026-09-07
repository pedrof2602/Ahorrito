"""Comparación de una lista de compra completa entre cadenas.

Hay dos formas fáciles de que un comparador de canastas dé un número convincente
y equivocado, y este módulo existe para evitar las dos:

1. **Elegir mal el producto de cada línea.** Para "leche", el resultado más barato
   suele ser una chocolatada de 200 ml. Por eso acá no se ordena por precio: se
   respeta el orden de relevancia que devuelve la cadena y se ancla por EAN
   siempre que se pueda.
2. **Dejar ganar a la cadena incompleta.** A quien le faltan dos ítems gasta menos
   por eso, no por ser más barata. Por eso el ranking sale del *total comparable*
   —solo las líneas que todas las cadenas resolvieron— y no del total crudo.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.basket import (
    BasketComparison,
    BasketCompareRequest,
    BasketLine,
    BasketLineResult,
    ChainErrorOut,
    ChainTotal,
    LineMatch,
    MatchConfidence,
    first_per_chain,
)
from app.models.catalog import Freshness, Offer, PriceScope, Product, ProductOffer, Store
from app.services.ingest import IngestService
from app.services.providers.base import PriceProvider
from app.services.providers.cache import bypass_cache
from app.services.providers.registry import ProviderRegistry
from app.services.search import primary_stores, resolve_stores

logger = logging.getLogger(__name__)

_SKU_PREFIX = "sku:"


@dataclass(slots=True)
class _Candidate:
    """Un producto candidato dentro de una cadena, con su posición de relevancia."""

    offer: ProductOffer
    rank: int


def _key(product: Product) -> str:
    """Identidad del producto dentro de una cadena.

    El EAN es la única clave comparable entre cadenas. Sin él, el SKU sirve para
    deduplicar dentro de la misma cadena pero nunca para cruzar.
    """
    return product.ean or f"{_SKU_PREFIX}{product.sku_id}"


def _is_ean(key: str) -> bool:
    return not key.startswith(_SKU_PREFIX)


def _better(a: Offer, b: Offer) -> bool:
    """Entre dos ofertas del *mismo* producto en la misma cadena.

    Disponible gana siempre: un precio excelente de algo sin stock no es una
    oferta, es una trampa.
    """
    if a.available != b.available:
        return a.available
    return a.price_cents < b.price_cents


def _index(offers: Sequence[ProductOffer]) -> dict[str, _Candidate]:
    """Colapsa los candidatos de una cadena por producto.

    Una búsqueda puede devolver varias ofertas del mismo producto (varios items o
    sellers). Colapsarlas es legítimo —mismo EAN, mismo producto— y evita que un
    seller marginal desplace al principal en el ranking.
    """
    index: dict[str, _Candidate] = {}
    for rank, offer in enumerate(offers):
        key = _key(offer.product)
        current = index.get(key)
        if current is None:
            index[key] = _Candidate(offer=offer, rank=rank)
            continue
        current.rank = min(current.rank, rank)
        if _better(offer.offer, current.offer.offer):
            current.offer = offer
    return index


def _best_by_relevance(index: Mapping[str, _Candidate]) -> _Candidate | None:
    """El candidato mejor rankeado, prefiriendo los que tienen stock."""
    if not index:
        return None
    return min(
        index.values(), key=lambda c: (not c.offer.offer.available, c.rank)
    )


def _pick_common_ean(
    indexes: Mapping[str, dict[str, _Candidate]],
) -> str | None:
    """EAN presente en todas las cadenas que respondieron algo.

    Entre varios candidatos gana el de **mejor relevancia combinada**, no el más
    barato: elegir el más barato del pool vuelve a meter la chocolatada de 200 ml,
    ahora disfrazada de "EAN común". El precio solo desempata.
    """
    answering = [slug for slug, index in indexes.items() if index]
    if len(answering) < 2:
        # Con una sola cadena no hay nada "común" que verificar; decir que la
        # coincidencia es exacta sería inventar una garantía que no tenemos.
        return None

    common = set.intersection(
        *({key for key in indexes[slug] if _is_ean(key)} for slug in answering)
    )
    if not common:
        return None

    def score(ean: str) -> tuple[int, int, int, str]:
        picks = [indexes[slug][ean] for slug in answering]
        return (
            0 if all(p.offer.offer.available for p in picks) else 1,
            sum(p.rank for p in picks),
            sum(p.offer.offer.price_cents for p in picks),
            ean,
        )

    return min(common, key=score)


def select_line(
    line: BasketLine,
    candidates: Mapping[str, Sequence[ProductOffer]],
    chain_slugs: Sequence[str],
) -> BasketLineResult:
    """Resuelve qué producto representa a esta línea en cada cadena.

    `candidates` trae los resultados en el orden de relevancia de cada cadena;
    `chain_slugs` son las cadenas que compiten en el ranking.
    """
    indexes = {slug: _index(candidates.get(slug) or ()) for slug in chain_slugs}
    picks: dict[str, _Candidate] = {}

    if line.ean:
        confidence = MatchConfidence.PINNED
        common_ean: str | None = line.ean
        for slug, index in indexes.items():
            if (candidate := index.get(line.ean)) is not None:
                picks[slug] = candidate
    elif (common_ean := _pick_common_ean(indexes)) is not None:
        # Solo participan las cadenas que tienen ese EAN. Completar las otras con
        # una coincidencia por nombre mezclaría productos distintos dentro de una
        # línea y volvería sin sentido su `saving_cents`.
        confidence = MatchConfidence.EXACT_EAN
        for slug, index in indexes.items():
            if (candidate := index.get(common_ean)) is not None:
                picks[slug] = candidate
    else:
        confidence = MatchConfidence.NAME
        for slug, index in indexes.items():
            if (candidate := _best_by_relevance(index)) is not None:
                picks[slug] = candidate

    matches = [
        LineMatch(
            chain_slug=slug,
            product=candidate.offer.product,
            offer=candidate.offer.offer,
            line_total_cents=candidate.offer.offer.price_cents * line.quantity,
            freshness=candidate.offer.freshness,
        )
        for slug, candidate in picks.items()
    ]
    # El stock manda sobre el precio, igual que en `_better`: una cadena no gana
    # una línea con un producto que no te podés llevar. Sin esto, el más barato
    # sin stock encabeza la línea y `saving_cents` anuncia un ahorro irreal.
    matches.sort(key=lambda m: (not m.offer.available, m.line_total_cents))

    in_stock = [m for m in matches if m.offer.available]
    totals = [m.line_total_cents for m in in_stock]
    comparable = len(picks) == len(chain_slugs) and all(
        c.offer.offer.available for c in picks.values()
    )

    return BasketLineResult(
        query=line.query,
        quantity=line.quantity,
        confidence=confidence,
        ean=common_ean if confidence is not MatchConfidence.NAME else None,
        matches=matches,
        missing_in=[slug for slug in chain_slugs if slug not in picks],
        comparable=comparable,
        # Sin ninguna cadena con stock no hay "más barata": el precio de algo que
        # nadie tiene no le gana a nada.
        cheapest_chain=in_stock[0].chain_slug if in_stock else None,
        saving_cents=max(totals) - min(totals) if len(totals) > 1 else 0,
    )


_SCOPE_RANK = {PriceScope.STORE: 2, PriceScope.CHANNEL: 1, PriceScope.NATIONAL: 0}


def _scope_of(matches: Iterable[LineMatch]) -> PriceScope:
    """El nivel del total es el de su línea menos precisa.

    Un total mezclado —dos líneas del canal de tu sucursal y una nacional— vale
    lo que vale la nacional. Declarar el nivel del mejor componente sería prometer
    una precisión que el total no tiene.
    """
    scopes = [m.offer.price_scope for m in matches]
    if not scopes:
        return PriceScope.NATIONAL
    return min(scopes, key=lambda s: _SCOPE_RANK[s])


def aggregate(
    lines: Sequence[BasketLineResult],
    providers: Sequence[PriceProvider],
    stores: Mapping[str, Store],
    verified: Mapping[str, int] | None = None,
) -> list[ChainTotal]:
    """Totaliza por cadena y arma el ranking.

    Ordena por **cuántas líneas resuelve** y recién después por total comparable.
    El total comparable sigue siendo el número con el que se comparan dos
    cadenas —solo mira las líneas que todas tienen, así que compara peras con
    peras—, pero por sí solo no alcanza para rankear: como excluye la línea que
    a una cadena le falta, la cadena incompleta compite sin el ítem que no tiene
    y puede salir primera justamente por eso. Que gane la que no te vende lo que
    fuiste a comprar es una recomendación equivocada: ese producto lo vas a
    pagar igual en otro lado, y el total que lo omite no es el precio de tu
    compra.

    Un producto sin stock cuenta como no resuelto: no poder llevártelo y no
    tenerlo son lo mismo del lado del changuito.
    """
    verified = verified or {}
    totals: list[ChainTotal] = []

    for provider in providers:
        slug = provider.chain.slug
        by_chain = {
            line.query: match
            for line in lines
            for match in line.matches
            if match.chain_slug == slug
        }
        totals.append(
            ChainTotal(
                chain_slug=slug,
                display_name=provider.chain.display_name,
                comparable_total_cents=sum(
                    match.line_total_cents
                    for line in lines
                    if line.comparable
                    for match in line.matches
                    if match.chain_slug == slug
                ),
                total_cents=sum(m.line_total_cents for m in by_chain.values()),
                verified_total_cents=verified.get(slug),
                matched_lines=len(by_chain),
                missing_lines=[
                    line.query for line in lines if line.query not in by_chain
                ],
                unavailable_lines=[
                    query
                    for query, match in by_chain.items()
                    if not match.offer.available
                ],
                price_scope=_scope_of(by_chain.values()),
                freshness=Freshness.worst(m.freshness for m in by_chain.values()),
                store=stores.get(slug),
            )
        )

    totals.sort(key=lambda t: (len(t.unresolved_lines), t.comparable_total_cents))
    return totals


async def _fan_out(
    providers: Sequence[PriceProvider],
    queries: Sequence[str],
    stores: Mapping[str, Store],
    limit: int,
    sales_channel: int | None = None,
) -> tuple[dict[str, dict[str, list[ProductOffer]]], list[ChainErrorOut], set[str]]:
    """Busca cada consulta en cada cadena, todo en paralelo.

    Sin semáforo propio: el `AsyncRateLimiter` del cliente ya serializa por cadena.
    Una segunda capa de límite acá solo lo haría más lento sin proteger nada.

    Devuelve los resultados, los errores, y las cadenas que fallaron en *todas*
    sus consultas —esas no pueden competir en el ranking—.
    """
    jobs = [(query, provider) for query in queries for provider in providers]
    responses = await asyncio.gather(
        *(
            provider.search(
                query,
                store=stores.get(provider.chain.slug),
                sales_channel=sales_channel,
                limit=limit,
            )
            for query, provider in jobs
        ),
        return_exceptions=True,
    )

    results: dict[str, dict[str, list[ProductOffer]]] = {q: {} for q in queries}
    errors: list[ChainErrorOut] = []
    failures: dict[str, int] = {}

    for (query, provider), response in zip(jobs, responses, strict=True):
        slug = provider.chain.slug
        if isinstance(response, BaseException):
            logger.warning("Falló '%s' en %s: %s", query, slug, response)
            errors.append(ChainErrorOut(chain_slug=slug, message=str(response)))
            failures[slug] = failures.get(slug, 0) + 1
            continue
        results[query][slug] = response

    dead = {slug for slug, count in failures.items() if count == len(queries)}
    return results, errors, dead


async def _verify_totals(
    providers: Sequence[PriceProvider],
    lines: Sequence[BasketLineResult],
    stores: Mapping[str, Store],
    errors: list[ChainErrorOut],
    sales_channel: int | None = None,
) -> dict[str, int]:
    """Total confirmado contra el checkout de cada cadena.

    Es el precio que realmente pagarías: el checkout aplica las promociones que el
    catálogo solo anuncia. Si falla, se degrada al total de catálogo en lugar de
    romper la respuesta.

    **Solo se informa un total si el checkout confirmó todos los SKU.** Sumar lo
    que sí confirmó daría un total más barato por estar incompleto, que es
    exactamente el número convincente y equivocado que este módulo evita.
    """
    verified: dict[str, int] = {}

    for provider in providers:
        slug = provider.chain.slug
        quantities: dict[str, int] = {}
        for line in lines:
            for match in line.matches:
                if match.chain_slug == slug:
                    quantities[match.product.sku_id] = (
                        quantities.get(match.product.sku_id, 0) + line.quantity
                    )
        if not quantities:
            continue
        try:
            confirmed = await provider.verify_prices(
                list(quantities.items()),
                store=stores.get(slug),
                sales_channel=sales_channel,
            )
        except Exception as exc:  # noqa: BLE001 - degradar, no romper
            logger.warning("No se pudo confirmar la canasta en %s: %s", slug, exc)
            errors.append(
                ChainErrorOut(chain_slug=slug, message=f"checkout no disponible: {exc}")
            )
            continue

        confirmed_prices = {item.product.sku_id: item.offer.price_cents for item in confirmed}
        if missing := set(quantities) - set(confirmed_prices):
            logger.warning(
                "%s: el checkout no confirmó %d de %d SKU; se informa solo el "
                "total de catálogo",
                slug,
                len(missing),
                len(quantities),
            )
            continue

        verified[slug] = sum(
            price * quantities[sku_id] for sku_id, price in confirmed_prices.items()
        )

    return verified


async def _persist(
    providers: Sequence[PriceProvider],
    lines: Sequence[BasketLineResult],
    stores: Mapping[str, Store],
    session: AsyncSession,
) -> None:
    """Guarda solo las ofertas elegidas.

    Persistir los ~600 candidatos de una lista de 15 líneas costaría segundos de
    SQLite en una operación interactiva. Lo que le interesa al histórico es qué
    salía lo que efectivamente comprarías.

    La `freshness` se reconstruye junto con la oferta para que `save_offers`
    pueda descartar las que salieron de la caché: ya están en el histórico, y
    volver a guardarlas correría su `last_seen_at` hacia atrás.
    """
    service = IngestService(session)
    for provider in providers:
        slug = provider.chain.slug
        chosen: dict[str, ProductOffer] = {}
        for line in lines:
            for match in line.matches:
                if match.chain_slug == slug:
                    chosen[match.product.sku_id] = ProductOffer(
                        product=match.product,
                        offer=match.offer,
                        freshness=match.freshness,
                    )
        if chosen:
            await service.save_offers(
                provider, list(chosen.values()), store=stores.get(slug)
            )


async def compare_basket(
    registry: ProviderRegistry,
    session: AsyncSession,
    request: BasketCompareRequest,
) -> BasketComparison:
    providers = [
        p
        for p in registry.all()
        if not request.chain_slugs or p.chain.slug in request.chain_slugs
    ]
    if not providers:
        return BasketComparison(
            comparable_line_count=0,
            total_line_count=len(request.lines),
            partial=False,
        )

    stores: dict[str, Store] = {}
    store_errors: list[ChainErrorOut] = []
    if request.postal_code:
        found, failures = await resolve_stores(providers, session, request.postal_code)
        stores = primary_stores(found)
        store_errors = [
            ChainErrorOut(chain_slug=f.chain_slug, message=f.message) for f in failures
        ]

    # Una lista puede repetir un término en dos líneas; se busca una sola vez.
    queries = list(dict.fromkeys(line.query for line in request.lines))
    with bypass_cache(request.fresh):
        results, errors, dead = await _fan_out(
            providers, queries, stores, request.candidates_per_chain, request.sales_channel
        )
    # Delante de los de búsqueda: no haber ubicado la sucursal es lo que explica
    # que los precios de esa cadena sean los nacionales, y `first_per_chain` se
    # queda con el primero de cada una.
    errors = store_errors + errors

    # Una cadena que falló en todo no compite: dejarla adentro vaciaría la canasta
    # comparable y arruinaría el ranking de las que sí respondieron.
    #
    # Con la caché por delante, "falló en todo" pasó a significar algo más
    # estricto: falló Y no teníamos ni un precio viejo suyo. Una cadena caída de
    # la que sí tenemos precios entra al ranking con ellos, marcada `stale`.
    # Sacarla también sería un resultado equivocado —y peor, porque no se ve—.
    ranked = [p for p in providers if p.chain.slug not in dead]
    chain_slugs = [p.chain.slug for p in ranked]

    lines = [
        select_line(line, results.get(line.query, {}), chain_slugs)
        for line in request.lines
    ]

    verified = (
        await _verify_totals(ranked, lines, stores, errors, request.sales_channel)
        if request.verify
        else {}
    )
    totals = aggregate(lines, ranked, stores, verified)

    if request.persist:
        await _persist(ranked, lines, stores, session)

    comparable_count = sum(1 for line in lines if line.comparable)
    winner = totals[0] if totals and comparable_count else None

    if winner is not None and not winner.complete:
        # Ganó por ser la menos incompleta, no por resolver la lista. Se informa,
        # pero su total no es el precio de la compra: lo que le falta se paga
        # igual en otro lado.
        logger.warning(
            "Ninguna cadena resuelve la lista entera; encabeza %s con %d línea(s) "
            "sin resolver: %s",
            winner.chain_slug,
            len(winner.unresolved_lines),
            ", ".join(winner.unresolved_lines),
        )

    return BasketComparison(
        chains=totals,
        lines=lines,
        comparable_line_count=comparable_count,
        total_line_count=len(lines),
        winner=winner.chain_slug if winner else None,
        winner_complete=bool(winner and winner.complete),
        partial=bool(errors),
        stale=any(t.freshness is not None and t.freshness.stale for t in totals),
        errors=first_per_chain(errors),
    )
