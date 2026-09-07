"""Implementación de `PriceProvider` sobre la API de Constructor.io de Coto.

Cumple el `Protocol` de `providers.base` por estructura, sin heredar: es a
propósito, y está explicado en ese módulo — Coto no comparte una línea de
implementación con VTEX, así que una base común le daría una cadena de herencia
que no quiere.

Lo que Coto **no** tiene es tan importante como lo que tiene: sin checkout
público no hay simulación de canasta ni precio autoritativo. Esos dos métodos
devuelven la respuesta honesta (`None` y `[]`) en lugar de inventar un total,
igual que hace Disco.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime

from app.models.catalog import (
    BasketSimulation,
    CategoryNode,
    Chain,
    ProductOffer,
    Store,
)

from . import mapper
from .client import CotoClient
from .config import CotoConfig

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(UTC)


class CotoProvider:
    """Proveedor de precios de Coto."""

    def __init__(self, config: CotoConfig, client: CotoClient) -> None:
        self.config = config
        self._client = client

    @property
    def chain(self) -> Chain:
        return Chain(
            slug=self.config.slug,
            display_name=self.config.display_name,
            supports_store_prices=self.config.supports_store_prices,
            # Constructor.io no tiene canales de venta: la palanca de precio es
            # la sucursal, y viaja entera en cada producto.
            sales_channels=(),
            default_sales_channel=None,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def find_stores(self, postal_code: str) -> list[Store]:
        """Coto no expone qué sucursal sirve a un código postal.

        Devuelve una lista vacía, que el `Protocol` declara como respuesta
        legítima. Los ids de sucursal *sí* se conocen —vienen en cada producto y
        `mapper.map_stores` los extrae—, pero sin nombre, dirección ni código
        postal no se puede afirmar que alguno le quede cerca a nadie. Inventar
        esa relación sería peor que no ofrecerla: el usuario elegiría una
        sucursal creyendo que es la suya.

        Quien ya sabe su número de sucursal puede pasarlo igual: `search` acepta
        un `Store` y devuelve su precio con `PriceScope.STORE`.
        """
        return []

    async def search(
        self,
        term: str,
        *,
        store: Store | None = None,
        sales_channel: int | None = None,
        limit: int = 50,
    ) -> list[ProductOffer]:
        """Busca por texto o por EAN.

        `sales_channel` se acepta y se ignora: es parte del contrato común y
        Coto no tiene canales. Ignorarlo es caer al precio disponible, que es lo
        que el `Protocol` pide, en lugar de fallar y dejar al usuario sin los
        precios de Coto por haber pedido algo de VTEX.
        """
        store_id, _ = self.config.resolve_price_scope(store)
        results, _ = await self._client.search(term, limit=limit)
        offers = mapper.map_product_offers(
            results,
            self.config,
            captured_at=_now(),
            store=store,
            store_id=store_id,
            drop_unmatched=self.config.drop_unmatched_results,
        )
        return offers[:limit]

    async def search_by_ean(
        self, ean: str, *, store: Store | None = None
    ) -> ProductOffer | None:
        """Producto exacto por código de barras.

        Constructor.io indexa el EAN como término, así que es la misma búsqueda;
        existe como método aparte porque el llamador quiere un producto, no una
        lista. Se verifica que el EAN devuelto sea el pedido: la búsqueda cae a
        similitud semántica cuando no encuentra nada, y un producto parecido
        devuelto como si fuera el pedido es el error caro.
        """
        offers = await self.search(ean, store=store, limit=5)
        normalized = mapper.normalize_ean(ean)
        for offer in offers:
            if normalized and offer.product.ean == normalized:
                return offer
        return None

    async def category_tree(self, depth: int = 3) -> tuple[CategoryNode, ...]:
        groups = await self._client.category_groups(depth)
        return mapper.map_category_tree(groups)

    async def iter_category(
        self,
        category: CategoryNode,
        *,
        store: Store | None = None,
        sales_channel: int | None = None,
    ) -> AsyncIterator[ProductOffer]:
        """Recorre una categoría entera.

        Si la categoría excede el tope de páginas, avisa en el log en lugar de
        cortar en silencio: perder catálogo sin decirlo es el peor modo de falla
        de un comparador.
        """
        store_id, _ = self.config.resolve_price_scope(store)
        group_id = mapper.int_to_group_id(category.id)
        collected = 0
        declared: int | None = None

        async for page in self._client.iter_group_pages(group_id):
            if page.truncated:
                logger.warning(
                    "%s: la categoría %s (%s) superó el tope de %d páginas; "
                    "hay que subdividirla para cubrirla entera",
                    self.config.slug,
                    category.name,
                    group_id,
                    self.config.max_pages,
                )
                break
            if page.total is not None:
                declared = page.total
            captured_at = _now()
            for offer in mapper.map_product_offers(
                page.items,
                self.config,
                captured_at=captured_at,
                store=store,
                store_id=store_id,
            ):
                collected += 1
                yield offer

        if declared is not None and collected < declared:
            logger.info(
                "%s: categoría %s devolvió %d de %d productos declarados",
                self.config.slug,
                category.name,
                collected,
                declared,
            )

    async def verify_prices(
        self,
        sku_quantities: Sequence[tuple[str, int]],
        *,
        store: Store | None = None,
        sales_channel: int | None = None,
        card_bin: str | None = None,
    ) -> list[ProductOffer]:
        """Coto no tiene checkout público que confirme precios.

        Devuelve `[]`: el llamador cae al precio de catálogo y lo declara como
        tal. Devolver los precios de catálogo desde acá sería peor que no
        contestar — los presentaría como confirmados contra caja cuando nadie
        los confirmó.
        """
        return []

    async def simulate_basket(
        self,
        sku_quantities: Sequence[tuple[str, int]],
        *,
        store: Store | None = None,
        sales_channel: int | None = None,
        card_bin: str | None = None,
    ) -> BasketSimulation | None:
        """Coto no tiene motor de promociones consultable.

        `None` es la respuesta que el `Protocol` define para este caso. Importa
        especialmente acá: las promos de Coto que sí conocemos son por cantidad
        ("50% 2da", "2x1") y con tarjeta, y sin checkout no hay forma honesta de
        saber cuáles se acumulan. Aplicarlas nosotros al total inventaría un
        ahorro que en la caja no aparece.
        """
        return None
