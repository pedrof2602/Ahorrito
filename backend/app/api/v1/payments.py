"""Endpoints de medios de pago y promociones.

La pregunta que contestan, entera: *con qué tarjeta, en qué cadena y qué día*
conviene comprar esta canasta.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import CurrentUser
from app.core.db import get_db
from app.db.repositories import (
    PaymentInstrumentRepository,
    PromoUsageRepository,
    to_domain_instrument,
)
from app.models.payment import (
    BestPaymentRequest,
    BestPaymentResult,
    BinSource,
    BinVerificationResult,
    CardKind,
    Channel,
    PaymentInstrument,
    PaymentPromoRule,
    PaymentRail,
    clean_bins,
    period_start_for,
)
from app.services.payment import best_payment, verify_bin
from app.services.promo_catalog import IssuerBins, get_catalog
from app.services.providers.registry import ProviderRegistry

router = APIRouter()


def get_registry(request: Request) -> ProviderRegistry:
    return request.app.state.registry


DbSession = Annotated[AsyncSession, Depends(get_db)]
Registry = Annotated[ProviderRegistry, Depends(get_registry)]


class InstrumentCreate(BaseModel):
    """Alta de un medio de pago.

    **No pidas ni mandes el número de tarjeta.** `bins` son los primeros 6 a 8
    dígitos, que identifican al emisor y no a la persona, y es todo lo que la
    simulación necesita para decidir si una promo aplica. Si se omite, se toman
    los BIN conocidos del emisor.
    """

    label: str = Field(..., min_length=1, max_length=120, examples=["Visa Galicia"])
    issuer_slug: str = Field(..., examples=["galicia"])
    brand: str | None = Field(None, examples=["visa"])
    kind: CardKind = CardKind.CREDIT
    rails: list[PaymentRail] = Field(default_factory=lambda: [PaymentRail.CARD])
    bins: list[str] = Field(
        default_factory=list,
        description="Primeros 6 a 8 dígitos. Vacío = usar los del catálogo.",
        examples=[["507858"]],
    )


class InstrumentUpdate(BaseModel):
    label: str | None = Field(None, min_length=1, max_length=120)
    brand: str | None = None
    kind: CardKind | None = None
    rails: list[PaymentRail] | None = None
    bins: list[str] | None = None


class InstrumentOut(PaymentInstrument):
    verified_at: datetime | None = None
    verified_promotions: list[str] = Field(default_factory=list)


def _to_out(row) -> InstrumentOut:
    domain = to_domain_instrument(row)
    return InstrumentOut(
        **domain.model_dump(),
        verified_at=row.verified_at,
        verified_promotions=list(row.verified_promotions or []),
    )


# ------------------------------------------------------------- medios de pago


@router.get(
    "/payment-instruments",
    response_model=list[InstrumentOut],
    summary="Mis tarjetas y billeteras",
    tags=["pagos"],
)
async def list_instruments(
    session: DbSession, user: CurrentUser
) -> list[InstrumentOut]:
    rows = await PaymentInstrumentRepository(session, user.id).list()
    return [_to_out(row) for row in rows]


@router.post(
    "/payment-instruments",
    response_model=InstrumentOut,
    status_code=status.HTTP_201_CREATED,
    summary="Cargar un medio de pago",
    tags=["pagos"],
)
async def create_instrument(
    session: DbSession, user: CurrentUser, payload: InstrumentCreate
) -> InstrumentOut:
    catalog = get_catalog()

    bins = clean_bins(payload.bins)
    # Sin BIN propio se cae al del catálogo, y la fuente lo declara: un BIN de
    # tabla puede no ser el de *tu* plástico, y esa diferencia cambia el precio.
    source = BinSource.USER if bins else BinSource.CATALOG
    if not bins:
        bins = catalog.bins_for(payload.issuer_slug)

    instrument = PaymentInstrument(
        label=payload.label,
        issuer_slug=payload.issuer_slug,
        brand=payload.brand,
        kind=payload.kind,
        rails=frozenset(payload.rails),
        bins=bins,
        bin_source=source,
    )
    repo = PaymentInstrumentRepository(session, user.id)
    try:
        row = await repo.create(instrument)
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Ya tenés un medio de pago llamado «{payload.label}».",
        ) from exc
    return _to_out(row)


@router.patch(
    "/payment-instruments/{instrument_id}",
    response_model=InstrumentOut,
    summary="Editar un medio de pago",
    tags=["pagos"],
)
async def update_instrument(
    session: DbSession,
    user: CurrentUser,
    instrument_id: int,
    payload: InstrumentUpdate,
) -> InstrumentOut:
    repo = PaymentInstrumentRepository(session, user.id)
    row = await repo.by_id(instrument_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Medio de pago inexistente")

    changes: dict[str, object] = {}
    if payload.label is not None:
        changes["label"] = payload.label
    if payload.brand is not None:
        changes["brand"] = payload.brand
    if payload.kind is not None:
        changes["kind"] = str(payload.kind)
    if payload.rails is not None:
        changes["rails"] = [str(r) for r in payload.rails]
    if payload.bins is not None:
        # Cambiar el BIN invalida la verificación anterior: lo que se midió fue
        # el BIN viejo, y arrastrar ese sello sería afirmar algo no comprobado.
        changes["bins"] = list(clean_bins(payload.bins))
        changes["bin_source"] = str(BinSource.USER)
        changes["verified_at"] = None
        changes["verified_promotions"] = None

    await repo.update(row, changes)
    await session.commit()
    return _to_out(row)


@router.delete(
    "/payment-instruments/{instrument_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Borrar un medio de pago",
    tags=["pagos"],
)
async def delete_instrument(
    session: DbSession, user: CurrentUser, instrument_id: int
) -> None:
    repo = PaymentInstrumentRepository(session, user.id)
    row = await repo.by_id(instrument_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Medio de pago inexistente")
    await repo.delete(row)
    await session.commit()


@router.post(
    "/payment-instruments/{instrument_id}/verify",
    response_model=BinVerificationResult,
    summary="Probar el BIN contra el checkout real",
    tags=["pagos"],
)
async def verify_instrument(
    session: DbSession, user: CurrentUser, registry: Registry, instrument_id: int
) -> BinVerificationResult:
    """Simula una compra chica con y sin la tarjeta y compara.

    La diferencia entre las dos simulaciones es, literalmente, lo que hace esa
    tarjeta. Si activa algo, el medio de pago pasa a `verified`; si no activa
    nada **no** se marca como inválido: puede ser una tarjeta sin beneficios acá,
    o una promo que hoy no corre, y no hay forma de distinguirlo desde afuera.
    """
    repo = PaymentInstrumentRepository(session, user.id)
    row = await repo.by_id(instrument_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Medio de pago inexistente")
    if not row.bins:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Este medio de pago no tiene BIN: no pasa por la red de tarjetas y "
            "no hay nada que probar contra el checkout.",
        )

    result = await verify_bin(registry, row.bins[0])
    unlocked = sorted(
        {name for check in result.checks for name in check.promotions_unlocked}
    )
    await repo.mark_verified(row, unlocked, datetime.now(UTC))
    await session.commit()
    return result


# ---------------------------------------------------------------- catálogos


@router.get(
    "/payment-issuers",
    response_model=list[IssuerBins],
    summary="Emisores con BIN conocidos",
    tags=["pagos"],
)
async def list_issuers() -> list[IssuerBins]:
    """Emisores que ya tienen BIN medidos, para cargar una tarjeta sin tipearlos."""
    return list(get_catalog().issuers)


@router.get(
    "/payment-promos",
    response_model=list[PaymentPromoRule],
    summary="Promos bancarias vigentes",
    tags=["pagos"],
)
async def list_promos(
    on_date: Annotated[date | None, Query(description="Día a consultar. Vacío = hoy.")] = None,
    chain_slug: Annotated[str | None, Query()] = None,
) -> list[PaymentPromoRule]:
    """Las reglas curadas que aplican ese día.

    Son datos cargados a mano —ninguna API las publica—, así que cada una viaja
    con `verified_on`, `source_url` y `unverified` para que se pueda juzgar
    cuánto vale antes de usarla.
    """
    day = on_date or date.today()
    return [
        rule
        for rule in get_catalog().rules
        if rule.is_valid_on(day)
        and (
            chain_slug is None
            or rule.chain_slugs is None
            or chain_slug in rule.chain_slugs
        )
    ]


class PromoDay(BaseModel):
    """Lo mejor que hay un día puntual."""

    day: date
    weekday: str
    rules: list[PaymentPromoRule] = Field(default_factory=list)
    best_percent: float = Field(
        0, description="El porcentaje más alto disponible ese día."
    )
    best_rule_id: str | None = None


_WEEKDAY_ES = (
    "lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo",
)


@router.get(
    "/promos/calendar",
    response_model=list[PromoDay],
    summary="Qué conviene cada día de la semana",
    tags=["pagos"],
)
async def promo_calendar(
    session: DbSession,
    user: CurrentUser,
    days: Annotated[int, Query(ge=1, le=31)] = 7,
    chain_slug: Annotated[str | None, Query()] = None,
    channel: Annotated[Channel, Query()] = Channel.IN_STORE,
) -> list[PromoDay]:
    """Proyecta las promos bancarias sobre los próximos días.

    Solo cubre las reglas curadas: las promos de la cadena no se pueden proyectar
    porque el checkout únicamente contesta por hoy. Para saber el número exacto
    de un día, hay que pedir `/basket/best-payment` ese día.

    El porcentaje **no** ordena por sí solo cuál conviene: un 35% con tope de
    $6.000 rinde menos que un 20% sin tope en una compra grande. Sirve para
    decidir qué día ir, no cuánto vas a ahorrar.
    """
    catalog = get_catalog()
    instruments = [
        to_domain_instrument(row)
        for row in await PaymentInstrumentRepository(session, user.id).list()
    ]
    today = date.today()
    calendar: list[PromoDay] = []

    for offset in range(days):
        day = today + timedelta(days=offset)
        matching: dict[str, PaymentPromoRule] = {}
        for instrument in instruments:
            for rule in catalog.rules_for(
                instrument,
                chain_slug=chain_slug or "",
                channel=channel,
                on_date=day,
            ):
                matching[rule.id] = rule
        # Sin medios cargados se muestran todas las reglas vigentes: es la vista
        # útil para alguien que todavía no cargó nada.
        if not instruments:
            matching = {
                rule.id: rule
                for rule in catalog.rules
                if rule.is_valid_on(day)
                and channel in rule.channels
                and (
                    chain_slug is None
                    or rule.chain_slugs is None
                    or chain_slug in rule.chain_slugs
                )
            }

        rules = sorted(matching.values(), key=lambda r: r.percent, reverse=True)
        calendar.append(
            PromoDay(
                day=day,
                weekday=_WEEKDAY_ES[day.weekday()],
                rules=rules,
                best_percent=rules[0].percent if rules else 0,
                best_rule_id=rules[0].id if rules else None,
            )
        )
    return calendar


# --------------------------------------------------------------- la pregunta


@router.post(
    "/basket/best-payment",
    response_model=BestPaymentResult,
    summary="Con qué tarjeta y en qué cadena conviene comprar esta canasta",
    tags=["pagos"],
)
async def best_payment_endpoint(
    registry: Registry,
    session: DbSession,
    user: CurrentUser,
    payload: BestPaymentRequest,
) -> BestPaymentResult:
    """El precio real de la canasta según con qué pagues.

    En Carrefour el descuento de la tarjeta **se mide**: la canasta se simula dos
    veces contra el checkout, sin tarjeta y con el BIN, y la diferencia es el
    ahorro. En Disco no hay checkout consultable, así que su número sale del
    catálogo más las reglas curadas, y cada opción lo declara en `confidence`.

    Las promos bancarias (BNA, MODO, Cuenta DNI, MP) no las publica ninguna API:
    salen de `data/payment_promos.yaml`. Las que no estén contrastadas contra la
    fuente aparecen en `unverified_rules`.
    """
    repo = PaymentInstrumentRepository(session, user.id)
    rows = await repo.list()
    if payload.instrument_ids is not None:
        wanted = set(payload.instrument_ids)
        rows = [row for row in rows if row.id in wanted]
        if missing := wanted - {row.id for row in rows}:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                f"Medios de pago inexistentes: {sorted(missing)}",
            )

    instruments = [to_domain_instrument(row) for row in rows]
    return await best_payment(registry, session, payload, instruments, user_id=user.id)


class UsageIn(BaseModel):
    rule_id: str
    amount_cents: int = Field(..., gt=0)
    on_date: date | None = None


@router.post(
    "/promo-usage",
    summary="Registrar consumo de un tope",
    tags=["pagos"],
)
async def record_usage(
    session: DbSession, user: CurrentUser, payload: Annotated[UsageIn, Body()]
) -> dict[str, object]:
    """Descuenta de un tope lo que ya usaste.

    Sin esto, un tope mensual se calcula entero en cada compra y el ahorro
    informado es el de la primera compra del mes, repetido todo el mes.
    """
    rule = next((r for r in get_catalog().rules if r.id == payload.rule_id), None)
    if rule is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"Regla desconocida: {payload.rule_id}"
        )

    day = payload.on_date or date.today()
    period_start = period_start_for(rule.cap_period, day)
    row = await PromoUsageRepository(session, user.id).add(
        rule.id, period_start, payload.amount_cents
    )
    await session.commit()
    return {
        "rule_id": rule.id,
        "period_start": period_start,
        "used_cents": row.used_cents,
        "cap_cents": rule.cap_cents,
        "remaining_cents": (
            max(rule.cap_cents - row.used_cents, 0)
            if rule.cap_cents is not None
            else None
        ),
    }
