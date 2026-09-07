"""Implementación de `PriceProvider` sobre VTEX.

Una sola implementación sirve a todas las cadenas VTEX; lo que cambia entra por
`VTEXStoreConfig`.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from typing import Any

from app.models.catalog import (
    BasketSimulation,
    CategoryNode,
    Chain,
    Offer,
    PriceScope,
    Product,
    ProductOffer,
    Promotion,
    PromotionKind,
    Store,
)

from . import mapper
from .client import VTEXClient
from .config import VTEXStoreConfig
from .quirks import normalize_ean

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(UTC)


def _applied_promotions(raw: dict[str, Any]) -> tuple[Promotion, ...]:
    """Promos que el checkout activó de verdad.

    `ratesAndBenefitsData` es distinto de los teasers del catálogo: los teasers
    anuncian lo que *podría* aplicar, esto informa lo que aplicó. Cuando se
    simula con tarjeta, la diferencia entre ambas listas es la promo que la
    tarjeta desplazó.
    """
    entries = (raw.get("ratesAndBenefitsData") or {}).get(
        "rateAndBenefitsIdentifiers", []
    )
    return tuple(
        Promotion(name=entry["name"], kind=PromotionKind.OTHER)
        for entry in entries
        if isinstance(entry, dict) and entry.get("name")
    )


def _totals(raw: dict[str, Any]) -> tuple[int, int]:
    """`(items_total, descuento)` en centavos, el descuento como positivo.

    Se leen solo `Items` y `Discounts` y no la suma de todo el array: el checkout
    también devuelve envío y `minimumOrderValue`, que no son parte de lo que
    cuesta la mercadería y meterían ruido al comparar cadenas.
    """
    by_id = {
        entry.get("id"): entry.get("value") or 0
        for entry in (raw.get("totals") or [])
        if isinstance(entry, dict)
    }
    return int(by_id.get("Items") or 0), abs(int(by_id.get("Discounts") or 0))


class VTEXProvider:
    """Proveedor de precios para una tienda VTEX."""

    def __init__(self, config: VTEXStoreConfig, client: VTEXClient) -> None:
        self.config = config
        self._client = client

    @property
    def chain(self) -> Chain:
        return Chain(
            slug=self.config.slug,
            display_name=self.config.display_name,
            supports_store_prices=self.config.supports_store_prices,
            sales_channels=self.config.sales_channels,
            default_sales_channel=self.config.default_sales_channel,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    def _resolve(
        self, store: Store | None, sales_channel: int | None
    ) -> tuple[int | None, PriceScope]:
        """Canal a usar y nivel de precisión realmente alcanzado."""
        return self.config.resolve_price_scope(sales_channel, store)

    def _simulation_seller(self, store: Store | None) -> str:
        """Seller con el que se simula.

        El de la sucursal solo se usa si la cadena demostró que funciona: en
        Carrefour apaga el motor de promociones y devuelve la canasta a precio
        de lista sin avisar. Ver `simulation_uses_store_seller`.
        """
        if store is not None and self.config.simulation_uses_store_seller:
            return store.external_id
        return "1"

    async def find_stores(self, postal_code: str) -> list[Store]:
        if not self.config.supports_region_discovery:
            return []
        raw = await self._client.regions(postal_code)
        return mapper.map_regions_to_stores(raw, self.config, postal_code)

    async def search(
        self,
        term: str,
        *,
        store: Store | None = None,
        sales_channel: int | None = None,
        limit: int = 50,
    ) -> list[ProductOffer]:
        channel, scope = self._resolve(store, sales_channel)
        raw_products = await self._client.search(
            term, sales_channel=channel, limit=limit
        )
        captured_at = _now()
        offers: list[ProductOffer] = []
        for raw in raw_products:
            offers.extend(
                mapper.map_product_offers(
                    raw, self.config, captured_at=captured_at, store=store, scope=scope
                )
            )
        return offers[:limit]

    async def category_tree(self, depth: int = 3) -> tuple[CategoryNode, ...]:
        raw = await self._client.category_tree(depth)
        return mapper.map_category_tree(raw, self.config)

    async def iter_category(
        self,
        category: CategoryNode,
        *,
        store: Store | None = None,
        sales_channel: int | None = None,
    ) -> AsyncIterator[ProductOffer]:
        """Recorre una categoría completa.

        Si la categoría excede el tope de offset, avisa en el log: perder
        catálogo en silencio es el peor modo de falla de un comparador.
        """
        sales_channel, scope = self._resolve(store, sales_channel)
        seen_total: int | None = None
        collected = 0

        async for page in self._client.iter_search_pages(
            category_fq=category.fq, sales_channel=sales_channel
        ):
            if page.truncated:
                logger.warning(
                    "%s: la categoría %s (%s) superó el tope de %d productos; "
                    "hay que subdividirla para cubrirla entera",
                    self.config.slug,
                    category.name,
                    category.fq,
                    self.config.max_offset,
                )
                break
            if page.total is not None:
                seen_total = page.total
            captured_at = _now()
            for raw in page.items:
                for offer in mapper.map_product_offers(
                    raw, self.config, captured_at=captured_at, store=store, scope=scope
                ):
                    collected += 1
                    yield offer

        if seen_total is not None and collected < seen_total:
            logger.info(
                "%s: categoría %s devolvió %d de %d productos declarados",
                self.config.slug,
                category.name,
                collected,
                seen_total,
            )

    async def simulate_basket(
        self,
        sku_quantities: Sequence[tuple[str, int]],
        *,
        store: Store | None = None,
        sales_channel: int | None = None,
        card_bin: str | None = None,
    ) -> BasketSimulation | None:
        """Total de la canasta según el motor de promociones de la cadena.

        Devuelve `None` si la cadena no tiene checkout consultable: es la
        respuesta honesta para Disco, que rechaza su propio catálogo.
        """
        if not self.config.supports_simulation or not sku_quantities:
            return None

        channel, _ = self._resolve(store, sales_channel)
        raw = await self._client.simulate(
            list(sku_quantities),
            seller_id=self._simulation_seller(store),
            postal_code=store.postal_code if store else None,
            sales_channel=channel,
            card_bin=card_bin,
        )

        items_total, discount = _totals(raw)
        confirmed = {
            str(item.get("id"))
            for item in (raw.get("items") or [])
            # Sin `sellingPrice` el ítem vino pero la sucursal no lo vende:
            # contarlo como confirmado daría un total al que le falta un producto.
            if item.get("sellingPrice") is not None
        }
        requested = {sku_id for sku_id, _ in sku_quantities}

        return BasketSimulation(
            items_total_cents=items_total,
            discount_cents=discount,
            total_cents=max(items_total - discount, 0),
            promotions=_applied_promotions(raw),
            card_bin=card_bin,
            confirmed_sku_ids=frozenset(confirmed),
            missing_sku_ids=frozenset(requested - confirmed),
        )

    async def verify_prices(
        self,
        sku_quantities: Sequence[tuple[str, int]],
        *,
        store: Store | None = None,
        sales_channel: int | None = None,
        card_bin: str | None = None,
    ) -> list[ProductOffer]:
        """Confirma precios contra checkout.

        Es la fuente autoritativa: aplica las promociones reales de la sucursal.
        Ojo con las unidades — acá los importes ya vienen en centavos, a
        diferencia de la búsqueda, que los da en pesos.
        """
        if not self.config.supports_simulation or not sku_quantities:
            return []

        channel, scope = self._resolve(store, sales_channel)
        seller_id = self._simulation_seller(store)
        raw = await self._client.simulate(
            list(sku_quantities),
            seller_id=seller_id,
            postal_code=store.postal_code if store else None,
            sales_channel=channel,
            card_bin=card_bin,
        )

        applied = _applied_promotions(raw)
        captured_at = _now()

        results: list[ProductOffer] = []
        for item in raw.get("items") or []:
            selling_price = item.get("sellingPrice")
            if selling_price is None:
                # La sucursal no vende ese SKU: viene el item, sin precio.
                continue
            list_price = item.get("listPrice")
            results.append(
                ProductOffer(
                    product=Product(
                        chain_slug=self.config.slug,
                        product_id=str(item.get("productId") or ""),
                        sku_id=str(item.get("id") or ""),
                        name=item.get("name") or "",
                        ean=normalize_ean(item.get("ean")),
                        image_url=item.get("imageUrl"),
                        measurement_unit=item.get("measurementUnit"),
                        unit_multiplier=float(item.get("unitMultiplier") or 1),
                    ),
                    offer=Offer(
                        # Ya vienen en centavos: no multiplicar.
                        price_cents=int(selling_price),
                        reference_price_cents=(
                            int(list_price)
                            if list_price and list_price > selling_price
                            else None
                        ),
                        available=item.get("availability") == "available",
                        available_quantity=int(item.get("quantity") or 0),
                        seller_id=str(item.get("seller") or seller_id),
                        price_scope=scope,
                        store_key=(
                            store.key if store and scope is PriceScope.STORE else None
                        ),
                        captured_at=captured_at,
                        promotions=applied,
                    ),
                )
            )
        return results
