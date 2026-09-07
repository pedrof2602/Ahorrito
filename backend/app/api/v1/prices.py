"""Endpoints de precios y comparación entre supermercados."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_db
from app.db.repositories import PriceRepository, ProductRepository
from app.models.basket import (
    BasketComparison,
    BasketCompareRequest,
    ChainErrorOut,
    first_per_chain,
)
from app.models.catalog import Chain, Freshness, PriceScope, ProductOffer, Store
from app.services.basket import compare_basket
from app.services.http import ProviderError, ProviderUnavailable
from app.services.ingest import IngestService
from app.services.providers.cache import bypass_cache
from app.services.providers.registry import ProviderRegistry
from app.services.search import (
    ChainError,
    group_by_ean,
    primary_stores,
    resolve_stores,
    search_all,
)

router = APIRouter()


class ChainFreshness(BaseModel):
    """Hace cuánto es el precio que se está mostrando de una cadena."""

    chain_slug: str
    freshness: Freshness


def _sources(offers: list[ProductOffer]) -> list[ChainFreshness]:
    """Cuándo se consultó cada cadena, para que la interfaz pueda decirlo.

    Va por cadena y no por oferta porque una búsqueda trae todos los resultados
    de una cadena en el mismo request: repetir el timestamp en cada producto
    sería el mismo dato N veces. El de cada oferta igual viaja en su
    `freshness`, para quien lo quiera al lado del precio.
    """
    by_chain: dict[str, list[Freshness | None]] = {}
    for item in offers:
        by_chain.setdefault(item.product.chain_slug, []).append(item.freshness)
    return [
        ChainFreshness(chain_slug=slug, freshness=worst)
        for slug, values in sorted(by_chain.items())
        if (worst := Freshness.worst(values)) is not None
    ]


def get_registry(request: Request) -> ProviderRegistry:
    return request.app.state.registry


DbSession = Annotated[AsyncSession, Depends(get_db)]
Registry = Annotated[ProviderRegistry, Depends(get_registry)]


class SearchResponse(BaseModel):
    term: str
    count: int
    partial: bool = Field(
        ...,
        description=(
            "True si alguna cadena falló: o faltan sus precios, o no se pudo "
            "ubicar su sucursal y los suyos son los nacionales. Cuál de las dos "
            "cosas pasó lo dice su entrada en `errors`."
        ),
    )
    results: list[ProductOffer]
    errors: list[ChainErrorOut] = []
    sources: list[ChainFreshness] = Field(
        default_factory=list,
        description=(
            "Cuándo se consultó cada cadena. Sirve para mostrar 'actualizado "
            "hace 3 horas' sin tener que recorrer los resultados."
        ),
    )
    stale: bool = Field(
        False,
        description=(
            "Alguna cadena no respondió y se sirvió su último precio conocido. "
            "Los resultados están completos, pero esos precios pueden estar "
            "desactualizados: cuál es cuál está en `sources` y en cada "
            "`freshness`."
        ),
    )


class ComparisonEntry(BaseModel):
    ean: str
    name: str
    offers: list[ProductOffer]
    cheapest_chain: str
    saving_cents: int = Field(
        ..., description="Diferencia entre la oferta más cara y la más barata."
    )
    freshness: Freshness | None = Field(
        None,
        description=(
            "La del precio menos fresco de la comparación: un ahorro calculado "
            "entre un precio de ahora y uno de hace ocho horas vale lo que vale "
            "el viejo."
        ),
    )


class HistoryPoint(BaseModel):
    price_cents: int
    reference_price_cents: int | None
    available: bool
    observed_at: datetime
    last_seen_at: datetime


@router.get("/chains", response_model=list[Chain], summary="Cadenas soportadas")
async def list_chains(registry: Registry) -> list[Chain]:
    return registry.chains()


@router.get("/stores", response_model=list[Store], summary="Sucursales cercanas")
async def list_stores(
    registry: Registry,
    session: DbSession,
    postal_code: Annotated[str | None, Query(description="Código postal")] = None,
) -> list[Store]:
    """Sucursales que sirven a un código postal.

    Las cadenas sin regionalización pública no aportan ninguna; eso no es un
    error, y por eso `Chain.supports_store_prices` lo declara de antemano.

    Una cadena caída no vacía la respuesta: se devuelven las sucursales de las
    que sí contestaron. Pero si **ninguna** contestó y hubo fallos, la lista
    vacía sería mentira —diría "no hay sucursales cerca" cuando lo que pasó es
    que no se pudo preguntar—, y ahí se responde 502.
    """
    code = postal_code or settings.DEFAULT_POSTAL_CODE
    by_chain, errors = await resolve_stores(registry.all(), session, code)
    found = [store for stores in by_chain.values() for store in stores]

    if errors and not found:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            "No se pudo consultar el localizador de sucursales de "
            f"{', '.join(e.chain_slug for e in errors)}. Volvé a intentar en un rato.",
        )
    return found


@router.get("/search", response_model=SearchResponse, summary="Buscar en todas las cadenas")
async def search(
    registry: Registry,
    session: DbSession,
    q: Annotated[str, Query(min_length=2, description="Término a buscar")],
    postal_code: Annotated[str | None, Query()] = None,
    sales_channel: Annotated[
        int | None,
        Query(
            description=(
                "Canal de venta. Publica precios distintos para el mismo producto; "
                "los disponibles están en /chains. Una cadena que no lo soporta "
                "cae a su precio nacional en lugar de fallar."
            )
        ),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
    persist: Annotated[bool, Query(description="Guardar los precios observados")] = True,
    fresh: Annotated[
        bool,
        Query(
            description=(
                "Ignorar la caché y consultar a las cadenas en vivo. Si una no "
                "responde igual se sirve su último precio conocido, marcado."
            )
        ),
    ] = False,
) -> SearchResponse:
    stores: dict[str, Store] = {}
    store_errors: list[ChainError] = []
    if postal_code:
        by_chain, store_errors = await resolve_stores(registry.all(), session, postal_code)
        stores = primary_stores(by_chain)

    with bypass_cache(fresh):
        result = await search_all(
            registry, q, stores=stores, limit_per_chain=limit, sales_channel=sales_channel
        )

    if persist and result.offers:
        service = IngestService(session)
        for provider in registry.all():
            chain_offers = [
                o for o in result.offers if o.product.chain_slug == provider.chain.slug
            ]
            if chain_offers:
                await service.save_offers(
                    provider, chain_offers, store=stores.get(provider.chain.slug)
                )

    sources = _sources(result.offers)
    # Los de sucursal van primero: explican por qué los precios de esa cadena son
    # los nacionales, y son la causa de la que después falla la búsqueda.
    errors = first_per_chain(
        ChainErrorOut(chain_slug=e.chain_slug, message=e.message)
        for e in [*store_errors, *result.errors]
    )
    return SearchResponse(
        term=q,
        count=len(result.offers),
        partial=bool(errors),
        results=result.offers,
        errors=errors,
        sources=sources,
        stale=any(s.freshness.stale for s in sources),
    )


@router.get(
    "/compare",
    response_model=list[ComparisonEntry],
    summary="Comparar el mismo producto entre cadenas",
)
async def compare(
    registry: Registry,
    session: DbSession,
    q: Annotated[str, Query(min_length=2)],
    postal_code: Annotated[str | None, Query()] = None,
    sales_channel: Annotated[int | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 30,
    fresh: Annotated[
        bool, Query(description="Ignorar la caché y consultar en vivo.")
    ] = False,
) -> list[ComparisonEntry]:
    """Solo devuelve productos con EAN presente en más de una cadena.

    Es la comparación honesta: sin EAN común no hay garantía de que se trate del
    mismo producto, y emparejar por nombre parecido llevaría a comparar peras
    con manzanas.

    **Los fallos de cadena acá solo quedan en el log**, porque la respuesta es una
    lista de comparaciones y no tiene dónde informarlos. Para saber qué cadena
    faltó hay que usar `/search`, que sí los devuelve en `errors`.
    """
    stores: dict[str, Store] = {}
    if postal_code:
        by_chain, _ = await resolve_stores(registry.all(), session, postal_code)
        stores = primary_stores(by_chain)

    with bypass_cache(fresh):
        result = await search_all(
            registry, q, stores=stores, limit_per_chain=limit, sales_channel=sales_channel
        )

    entries: list[ComparisonEntry] = []
    for ean, offers in group_by_ean(result.offers).items():
        chains = {o.product.chain_slug for o in offers}
        if len(chains) < 2:
            continue
        cheapest, dearest = offers[0], offers[-1]
        entries.append(
            ComparisonEntry(
                ean=ean,
                name=cheapest.product.name,
                offers=offers,
                cheapest_chain=cheapest.product.chain_slug,
                saving_cents=dearest.offer.price_cents - cheapest.offer.price_cents,
                freshness=Freshness.worst(o.freshness for o in offers),
            )
        )
    entries.sort(key=lambda e: e.saving_cents, reverse=True)
    return entries


@router.get(
    "/products/{ean}/history",
    response_model=list[HistoryPoint],
    summary="Histórico de precios de un producto",
)
async def price_history(
    session: DbSession,
    ean: str,
    days: Annotated[int, Query(ge=1, le=365)] = 90,
    chain_slug: Annotated[str | None, Query()] = None,
) -> list[HistoryPoint]:
    products = ProductRepository(session)
    prices = PriceRepository(session)

    skus = await products.skus_for_ean(ean)
    if not skus:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No hay datos para el EAN {ean}. Buscalo primero para poblarlo.",
        )

    since = datetime.now(UTC) - timedelta(days=days)
    points: list[HistoryPoint] = []
    for sku in skus:
        for snap in await prices.history(sku.id, since):
            points.append(
                HistoryPoint(
                    price_cents=snap.price_cents,
                    reference_price_cents=snap.reference_price_cents,
                    available=snap.available,
                    observed_at=snap.observed_at,
                    last_seen_at=snap.last_seen_at,
                )
            )
    points.sort(key=lambda p: p.observed_at)
    return points


class BasketItem(BaseModel):
    sku_id: str
    quantity: int = Field(1, ge=1)


class BasketRequest(BaseModel):
    chain_slug: str
    items: list[BasketItem]
    store_external_id: str | None = None
    postal_code: str | None = None


@router.post(
    "/basket/compare",
    response_model=BasketComparison,
    summary="Comparar una lista de compra completa entre cadenas",
)
async def compare_basket_endpoint(
    registry: Registry, session: DbSession, payload: BasketCompareRequest
) -> BasketComparison:
    """Cuánto sale tu lista de compra entera en cada supermercado.

    El ranking sale de `comparable_total_cents` —solo las líneas que todas las
    cadenas tienen—, no del total crudo: a quien le falten dos ítems le va a dar
    menos por eso, no por ser más barato.

    Cada línea declara su `confidence`: `exact_ean` significa que se comparó el
    mismo producto en ambas cadenas; `name`, que las coincidencias podrían diferir
    en marca o gramaje.
    """
    return await compare_basket(registry, session, payload)


@router.post(
    "/basket/verify",
    response_model=list[ProductOffer],
    summary="Confirmar precios de una canasta contra el checkout",
)
async def verify_basket(registry: Registry, payload: BasketRequest) -> list[ProductOffer]:
    """Precio autoritativo: es lo que realmente pagarías en esa sucursal.

    La búsqueda de catálogo alcanza para comparar, pero antes de decidir dónde
    comprar conviene confirmar contra checkout, que aplica las promos reales.

    Acá no hay degradación posible: es una sola cadena y el punto del endpoint es
    el número confirmado. Si el checkout no contesta, se dice —503 o 502 según de
    quién sea la culpa— en lugar de devolver el precio de catálogo haciéndolo
    pasar por confirmado.
    """
    provider = registry.get(payload.chain_slug)
    if provider is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Cadena desconocida: {payload.chain_slug}",
        )

    store = None
    if payload.store_external_id:
        store = Store(
            chain_slug=payload.chain_slug,
            external_id=payload.store_external_id,
            name=payload.store_external_id,
            postal_code=payload.postal_code,
        )
    try:
        return await provider.verify_prices(
            [(item.sku_id, item.quantity) for item in payload.items], store=store
        )
    except ProviderUnavailable as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"El checkout de {provider.chain.display_name} no está respondiendo "
            f"({exc}). Los precios de catálogo siguen sirviendo para comparar.",
        ) from exc
    except ProviderError as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"No se pudo confirmar el precio en {provider.chain.display_name}: {exc}",
        ) from exc
