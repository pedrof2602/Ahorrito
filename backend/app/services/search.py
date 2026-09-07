"""Búsqueda federada entre cadenas."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.catalog import ProductOffer, Store
from app.services.ingest import IngestService
from app.services.providers.base import PriceProvider
from app.services.providers.registry import ProviderRegistry

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ChainError:
    chain_slug: str
    message: str


@dataclass(slots=True)
class SearchResult:
    offers: list[ProductOffer] = field(default_factory=list)
    errors: list[ChainError] = field(default_factory=list)

    @property
    def partial(self) -> bool:
        return bool(self.errors)


async def resolve_stores(
    providers: Sequence[PriceProvider],
    session: AsyncSession,
    postal_code: str,
) -> tuple[dict[str, list[Store]], list[ChainError]]:
    """Sucursales de cada cadena para un código postal, tolerando las caídas.

    El mismo criterio que `search_all`, un paso antes. Sin esto el fallo del
    localizador subía sin guarda hasta el endpoint y devolvía un 500 entero:
    que Disco no conteste qué sucursal te toca costaba los precios de Carrefour,
    justo lo que la búsqueda federada está construida para evitar.

    Que falte la sucursal no cancela la consulta a esa cadena —se busca sin
    `store` y se cae al precio nacional—, pero tampoco se traga: quien pidió los
    precios de su barrio va a ver otros, y tiene que enterarse.

    Secuencial y no `asyncio.gather`: las cadenas comparten la `AsyncSession` y
    `sync_stores` escribe en ella, y una sesión de SQLAlchemy no admite uso
    concurrente.
    """
    service = IngestService(session)
    found: dict[str, list[Store]] = {}
    errors: list[ChainError] = []

    for provider in providers:
        slug = provider.chain.slug
        try:
            stores = await service.sync_stores(provider, postal_code)
        except Exception as exc:  # noqa: BLE001 - degradar, no romper
            logger.warning("No se pudo resolver la sucursal de %s: %s", slug, exc)
            errors.append(
                ChainError(
                    slug,
                    "no se pudo ubicar tu sucursal y se muestran los precios "
                    f"nacionales: {exc}",
                )
            )
            # Si lo que falló fue la escritura y no la cadena, la transacción
            # queda envenenada y la cadena siguiente moriría heredando un
            # `PendingRollbackError` que no tiene nada que ver con ella.
            await session.rollback()
            continue
        if stores:
            found[slug] = stores

    return found, errors


def primary_stores(by_chain: dict[str, list[Store]]) -> dict[str, Store]:
    """La primera sucursal de cada cadena, que es con la que se cotiza."""
    return {slug: stores[0] for slug, stores in by_chain.items() if stores}


async def search_all(
    registry: ProviderRegistry,
    term: str,
    *,
    stores: dict[str, Store] | None = None,
    limit_per_chain: int = 25,
    chain_slugs: list[str] | None = None,
    sales_channel: int | None = None,
) -> SearchResult:
    """Consulta todas las cadenas en paralelo.

    Si una cadena falla, las demás igual responden: devolver un 500 entero
    porque Disco se cayó dejaría al usuario sin los precios de Carrefour, que sí
    tenemos. El fallo se informa aparte, sin disfrazarlo de "no hay resultados".
    """
    providers = [
        p
        for p in registry.all()
        if not chain_slugs or p.chain.slug in chain_slugs
    ]
    stores = stores or {}

    responses = await asyncio.gather(
        *(
            p.search(
                term,
                store=stores.get(p.chain.slug),
                sales_channel=sales_channel,
                limit=limit_per_chain,
            )
            for p in providers
        ),
        return_exceptions=True,
    )

    result = SearchResult()
    for provider, response in zip(providers, responses, strict=True):
        if isinstance(response, BaseException):
            logger.warning("Falló la búsqueda en %s: %s", provider.chain.slug, response)
            result.errors.append(ChainError(provider.chain.slug, str(response)))
            continue
        result.offers.extend(response)

    result.offers.sort(key=lambda o: o.offer.price_cents)
    return result


def group_by_ean(offers: list[ProductOffer]) -> dict[str, list[ProductOffer]]:
    """Agrupa ofertas comparables entre cadenas.

    Solo agrupa lo que tiene EAN: es la única clave confiable. Un producto sin
    EAN se muestra igual, pero sin par en la otra cadena.
    """
    grouped: dict[str, list[ProductOffer]] = {}
    for item in offers:
        if item.product.ean:
            grouped.setdefault(item.product.ean, []).append(item)
    for entries in grouped.values():
        entries.sort(key=lambda o: o.offer.price_cents)
    return grouped
