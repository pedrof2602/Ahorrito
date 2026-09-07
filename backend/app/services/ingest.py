"""Ingesta: lleva lo que devuelven los proveedores a la base."""

from __future__ import annotations

import logging
from collections.abc import Iterable

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories import (
    ChainRepository,
    IngestResult,
    PriceRepository,
    ProductRepository,
    StoreRepository,
)
from app.models.catalog import CategoryNode, ProductOffer, Store
from app.services.providers.base import PriceProvider

logger = logging.getLogger(__name__)


class IngestService:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session
        self.chains = ChainRepository(session)
        self.stores = StoreRepository(session)
        self.products = ProductRepository(session)
        self.prices = PriceRepository(session)

    async def _store_row_id(self, chain_id: int, store: Store | None) -> int | None:
        if store is None:
            return None
        row = await self.stores.upsert(chain_id, store)
        return row.id

    async def save_offers(
        self,
        provider: PriceProvider,
        offers: Iterable[ProductOffer],
        *,
        store: Store | None = None,
    ) -> IngestResult:
        """Guarda en el histórico las ofertas que se trajeron en vivo.

        Las servidas de caché se descartan: ya se guardaron cuando se las trajo,
        y volver a pasarlas movería `last_seen_at` **hacia atrás** —el
        `captured_at` de una oferta cacheada es viejo, y `PriceRepository.record`
        lo copia tal cual cuando el precio no cambió—. El histórico quedaría
        diciendo que un precio dejó de verse antes de la última vez que se lo
        vio.
        """
        live = [
            item
            for item in offers
            if item.freshness is None or not item.freshness.from_cache
        ]
        if not live:
            return IngestResult()

        chain_row = await self.chains.upsert(provider.chain)
        store_id = await self._store_row_id(chain_row.id, store)
        result = IngestResult()

        for item in live:
            sku_row = await self.products.upsert_sku(
                chain_row.id, item.product, item.offer.captured_at
            )
            result.skus_seen += 1
            if await self.prices.record(sku_row.id, store_id, item.offer):
                result.snapshots_inserted += 1
            else:
                result.snapshots_touched += 1

        await self._s.commit()
        return result

    async def sync_stores(self, provider: PriceProvider, postal_code: str) -> list[Store]:
        """Descubre y persiste las sucursales que sirven a un código postal."""
        stores = await provider.find_stores(postal_code)
        if not stores:
            return []
        chain_row = await self.chains.upsert(provider.chain)
        for store in stores:
            await self.stores.upsert(chain_row.id, store)
        await self._s.commit()
        return stores

    async def crawl_category(
        self,
        provider: PriceProvider,
        category: CategoryNode,
        *,
        store: Store | None = None,
        commit_every: int = 200,
    ) -> IngestResult:
        """Recorre una categoría y persiste a medida que avanza.

        Commitea por lotes para que un corte a mitad de camino no tire todo el
        trabajo hecho: un crawl de catálogo entero dura y conviene que sea
        reanudable.
        """
        chain_row = await self.chains.upsert(provider.chain)
        store_id = await self._store_row_id(chain_row.id, store)
        result = IngestResult()

        async for item in provider.iter_category(category, store=store):
            sku_row = await self.products.upsert_sku(
                chain_row.id, item.product, item.offer.captured_at
            )
            result.skus_seen += 1
            if await self.prices.record(sku_row.id, store_id, item.offer):
                result.snapshots_inserted += 1
            else:
                result.snapshots_touched += 1

            if result.skus_seen % commit_every == 0:
                await self._s.commit()

        await self._s.commit()
        logger.info(
            "%s / %s: %d SKU, %d precios nuevos, %d sin cambios",
            provider.chain.slug,
            category.name,
            result.skus_seen,
            result.snapshots_inserted,
            result.snapshots_touched,
        )
        return result
