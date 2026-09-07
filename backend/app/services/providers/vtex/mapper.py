"""Traducción del JSON crudo de VTEX a los modelos canónicos.

Funciones puras: sin I/O, sin base de datos y sin reloj (`captured_at` entra por
parámetro). Así se pueden testear exhaustivamente contra fixtures grabadas, que
es donde aparecen los bugs de verdad.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any

from app.models.catalog import (
    CategoryNode,
    Offer,
    PriceScope,
    Product,
    ProductOffer,
    Promotion,
    PromotionKind,
    Store,
)

from app.services.providers.promo_text import membership_required, parse_promo_text

from .config import VTEXStoreConfig
from .quirks import normalize_ean, to_cents, unmangle

logger = logging.getLogger(__name__)

# Si ListPrice supera al precio por más de este factor, es dato basura.
# Disco devolvió ListPrice=252066 contra Price=3050 (factor ~82).
_MAX_PLAUSIBLE_LIST_RATIO = 10

_PERCENT_IN_NAME = re.compile(r"(\d{1,2})\s*%")


def _reference_price_cents(offer: dict[str, Any], price_cents: int) -> int | None:
    """Elige un precio tachado confiable, o ninguno.

    `PriceWithoutDiscount` es el campo consistente en ambas cadenas.
    `ListPrice` solo se usa si pasa un control de sanidad, porque Disco publica
    valores absurdos que producirían descuentos del 98% inexistentes.
    """
    without_discount = to_cents(offer.get("PriceWithoutDiscount"))
    if without_discount and without_discount > price_cents:
        return without_discount

    list_price = to_cents(offer.get("ListPrice"))
    if (
        list_price
        and list_price > price_cents
        and list_price < price_cents * _MAX_PLAUSIBLE_LIST_RATIO
    ):
        return list_price
    return None


def _promotion_kind(name: str, effects: dict[str, Any]) -> PromotionKind:
    params = {p.get("Name"): p.get("Value") for p in effects.get("Parameters", [])}
    if "PercentualDiscount" in params:
        lowered = name.lower()
        if any(word in lowered for word in ("tarjeta", "banco", "card", "cuota")):
            return PromotionKind.PAYMENT_METHOD
        return PromotionKind.PERCENT_DISCOUNT
    if "NominalDiscount" in params:
        return PromotionKind.NOMINAL_DISCOUNT
    if "BuyTogether" in params or "MaxQuantity" in params:
        return PromotionKind.BUY_X_GET_Y
    return PromotionKind.OTHER


def _parse_promotion(
    raw: dict[str, Any], *, unit_price_cents: int | None = None
) -> Promotion | None:
    """Traduce un teaser de VTEX a una promoción.

    **La mecánica sale del nombre, no de `Effects`.** Medido sobre Carrefour y
    DIA: los teasers de promo por cantidad llegan con `Parameters: []`, así que
    `Effects` no alcanza para distinguir un "2do al 70%" de un descuento común.
    Todo lo que el checkout va a aplicar está escrito en el título.
    """
    name = raw.get("Name")
    if not name:
        return None
    effects = raw.get("Effects") or {}
    conditions = raw.get("Conditions") or {}

    percent: float | None = None
    for param in effects.get("Parameters", []):
        if param.get("Name") == "PercentualDiscount":
            try:
                percent = float(param["Value"])
            except (KeyError, TypeError, ValueError):
                percent = None
            break
    if percent is None and (match := _PERCENT_IN_NAME.search(name)):
        percent = float(match[1])

    minimum = conditions.get("MinimumQuantity")
    minimum = minimum if isinstance(minimum, int) and minimum > 0 else None

    kind = _promotion_kind(name, effects)
    mechanics = parse_promo_text(name, unit_price_cents=unit_price_cents)
    if mechanics is not None:
        # La forma del título manda sobre la heurística de `Effects`: un
        # "2do al 70%" que traiga `PercentualDiscount` no es un 70% sobre la
        # compra, y dejarlo como PERCENT_DISCOUNT publicaría el doble de ahorro.
        # Excepción: si el nombre nombra una tarjeta, sigue siendo una promo de
        # medio de pago —"2do al 50% Mi Crf" exige la tarjeta para existir— y
        # esa condición pesa más que la mecánica para decidir si te alcanza.
        if kind is not PromotionKind.PAYMENT_METHOD:
            kind = mechanics.kind
        percent = None
        minimum = minimum or mechanics.bundle_quantity

    return Promotion(
        name=name,
        kind=kind,
        percent=percent,
        minimum_quantity=minimum,
        paid_quantity=mechanics.paid_quantity if mechanics else None,
        nth_unit_percent_off=mechanics.nth_unit_percent_off if mechanics else None,
        bundle_price_cents=mechanics.bundle_price_cents if mechanics else None,
        effective_percent=mechanics.effective_percent if mechanics else None,
        description=mechanics.describe() or None if mechanics else None,
        requires_membership=membership_required(name),
    )


def parse_promotions(
    offer: dict[str, Any],
    config: VTEXStoreConfig,
    *,
    unit_price_cents: int | None = None,
) -> tuple[Promotion, ...]:
    """Extrae promociones prefiriendo la variante sin mangling.

    `PromotionTeasers` viene con nombres limpios en ambas cadenas; `Teasers` es
    el que Carrefour serializa con los backing fields de C# al aire.

    `unit_price_cents` solo lo necesitan las promos de precio fijo ("2x$2500"):
    sin el precio de una unidad no se puede decir cuánto se ahorra.
    """
    raw_list = offer.get("PromotionTeasers")
    if not raw_list:
        raw_list = offer.get("Teasers") or []
        if config.teaser_backing_fields:
            raw_list = unmangle(raw_list)

    promotions = []
    for raw in raw_list:
        if isinstance(raw, dict) and (
            promo := _parse_promotion(raw, unit_price_cents=unit_price_cents)
        ):
            promotions.append(promo)
    return tuple(promotions)


def _price_per_unit_cents(price_cents: int, item: dict[str, Any]) -> int | None:
    """Precio por unidad de medida, para comparar 1L contra 900ml.

    Es la función central del producto y VTEX no la publica de forma confiable,
    así que la calculamos con el multiplicador del SKU.
    """
    try:
        multiplier = float(item.get("unitMultiplier") or 1)
    except (TypeError, ValueError):
        return None
    if multiplier <= 0 or multiplier == 1:
        return None
    return round(price_cents / multiplier)


def _image_url(item: dict[str, Any]) -> str | None:
    images = item.get("images") or []
    if images and isinstance(images[0], dict):
        return images[0].get("imageUrl")
    return None


def _category_path(raw: dict[str, Any]) -> tuple[str, ...]:
    categories = raw.get("categories") or []
    if not categories:
        return ()
    # VTEX las devuelve de la más específica a la más general: "/A/B/C/".
    deepest = max(categories, key=len)
    return tuple(part for part in deepest.split("/") if part)


def map_product_offers(
    raw: dict[str, Any],
    config: VTEXStoreConfig,
    *,
    captured_at: datetime,
    store: Store | None = None,
    scope: PriceScope = PriceScope.NATIONAL,
) -> list[ProductOffer]:
    """Mapea un producto crudo de VTEX a una oferta por SKU vendible.

    Un producto VTEX tiene N items (SKUs) y cada uno N sellers. Nos quedamos con
    el seller por defecto de cada SKU: los de marketplace aparecen en algunos
    sales channels y sus precios no son los de la cadena.

    El `scope` lo decide el proveedor con `resolve_price_scope`, no se deduce acá
    de si vino una sucursal: pedir un código postal no garantiza haber conseguido
    un precio de sucursal, y etiquetarlo así sería el error que `PriceScope`
    existe para evitar.
    """
    product_id = str(raw.get("productId") or "")
    if not product_id:
        return []

    category_path = _category_path(raw)
    results: list[ProductOffer] = []

    for item in raw.get("items") or []:
        sellers = item.get("sellers") or []
        if not sellers:
            continue
        seller = next((s for s in sellers if s.get("sellerDefault")), sellers[0])
        offer_raw = seller.get("commertialOffer") or {}

        price_cents = to_cents(offer_raw.get("Price"))
        if price_cents is None or price_cents <= 0:
            # Sin precio no hay nada que comparar. Pasa cuando una sucursal no
            # vende el SKU: el item viene igual, pero con Price nulo o cero.
            continue

        product = Product(
            chain_slug=config.slug,
            product_id=product_id,
            sku_id=str(item.get("itemId") or ""),
            name=item.get("nameComplete") or item.get("name") or raw.get("productName") or "",
            brand=raw.get("brand"),
            ean=normalize_ean(item.get("ean")),
            category_path=category_path,
            image_url=_image_url(item),
            link=raw.get("link"),
            measurement_unit=item.get("measurementUnit"),
            unit_multiplier=float(item.get("unitMultiplier") or 1),
        )

        available_quantity = int(offer_raw.get("AvailableQuantity") or 0)
        offer = Offer(
            price_cents=price_cents,
            reference_price_cents=_reference_price_cents(offer_raw, price_cents),
            available=bool(offer_raw.get("IsAvailable", available_quantity > 0)),
            available_quantity=available_quantity,
            seller_id=str(seller.get("sellerId") or ""),
            price_scope=scope,
            # Solo se nombra la sucursal si el precio es realmente suyo.
            store_key=store.key if store and scope is PriceScope.STORE else None,
            price_per_unit_cents=_price_per_unit_cents(price_cents, item),
            captured_at=captured_at,
            promotions=parse_promotions(
                offer_raw, config, unit_price_cents=price_cents
            ),
        )
        results.append(ProductOffer(product=product, offer=offer))

    return results


def map_category_tree(
    raw_nodes: list[dict[str, Any]],
    config: VTEXStoreConfig,
    _parent_path: tuple[int, ...] = (),
) -> tuple[CategoryNode, ...]:
    """Arma el árbol de categorías acumulando el path raíz->nodo.

    El path completo importa: `fq=C:` exige `/3/4/5/` y devuelve 0 resultados si
    se le pasa solo el id del nodo hoja.
    """
    nodes = []
    for raw in raw_nodes:
        node_id = raw.get("id")
        if node_id is None or node_id in config.excluded_category_ids:
            continue
        id_path = (*_parent_path, int(node_id))
        nodes.append(
            CategoryNode(
                id=int(node_id),
                name=raw.get("name") or "",
                id_path=id_path,
                children=map_category_tree(raw.get("children") or [], config, id_path),
            )
        )
    return tuple(nodes)


def map_regions_to_stores(
    raw_regions: list[dict[str, Any]],
    config: VTEXStoreConfig,
    postal_code: str,
) -> list[Store]:
    """Convierte la respuesta de `/regions` en sucursales.

    Cada `seller` de una región es una sucursal física con nombre legible
    ("Hiper Warnes", "Hiper Córdoba Colón").

    **`sales_channel` queda en None a propósito.** `/regions` identifica la
    sucursal pero no dice en qué canal de venta compra, y el canal es lo único
    que mueve el precio. Asignarles a todas el canal por defecto —como se hacía
    antes— devolvía el precio nacional con etiqueta de sucursal: dos códigos
    postales distintos daban el mismo total y parecía que Warnes y Córdoba Colón
    cobraban igual. Cuando se pueda medir la atribución, se llena acá y la cadena
    de resolución empieza a dar `PriceScope.STORE` sola.
    """
    stores: list[Store] = []
    seen: set[str] = set()
    for region in raw_regions:
        region_id = region.get("id")
        for seller in region.get("sellers") or []:
            seller_id = seller.get("id")
            if not seller_id or seller_id in seen:
                continue
            seen.add(seller_id)
            name = seller.get("name") or seller_id
            stores.append(
                Store(
                    chain_slug=config.slug,
                    external_id=seller_id,
                    # Algunas sucursales vienen sin nombre y repiten el id.
                    name=name if name != seller_id else f"Sucursal {seller_id}",
                    sales_channel=None,
                    region_id=region_id,
                    postal_code=postal_code,
                )
            )
    return stores
