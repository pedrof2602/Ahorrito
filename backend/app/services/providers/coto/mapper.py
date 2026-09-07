"""Traducción del JSON de Constructor.io a los modelos canónicos.

Funciones puras: sin I/O, sin base de datos y sin reloj (`captured_at` entra por
parámetro), igual que el mapper de VTEX y por el mismo motivo — así se testea
contra fixtures grabadas, que es donde aparecen los bugs de verdad.

Las tres decisiones de mapeo que no son obvias están documentadas en
`_shelf_price_cents`, `_unit_price_cents` y `parse_promotions`. Las tres salieron
de medir la API, no de leer documentación: en Constructor.io los nombres de los
campos no significan lo que parecen.
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
from app.services.providers.common import normalize_ean, to_cents
from app.services.providers.promo_text import membership_required, parse_promo_text

from .config import CotoConfig

logger = logging.getLogger(__name__)

_PERCENT = re.compile(r"(\d{1,3})\s*%")
_TAKING_QUANTITY = re.compile(r"(\d+)")
# "$2249.25", "Precio Contado: $2999". El punto es separador decimal y siempre
# trae dos dígitos; no hay separador de miles en estos campos.
_MONEY = re.compile(r"\$\s*(\d+(?:\.\d{1,2})?)")

# `group_id` de Constructor.io: "catv00001255". `CategoryNode.id` es int, así
# que se guarda la parte numérica y se reconstruye el id al navegar.
_GROUP_ID = re.compile(r"^([a-z]+)(\d+)$")
_GROUP_ID_PREFIX = "catv"
_GROUP_ID_DIGITS = 8


def parse_money(raw: str | float | int | None) -> float | None:
    """Extrae un importe de los campos de texto de las promos.

    Coto publica el precio con descuento sólo como string (`"$2249.25"`); el
    campo numérico `regularPrice` vino en `null` en las 26 promos medidas.

    **El punto es siempre separador decimal, nunca de miles.** Medido sobre 191
    importes de cuatro búsquedas, incluidos `"$1259988.00"` y `"$1449999.00"`:
    ninguno agrupa los miles. Agregarle manejo de separador de miles a esta
    función convertiría `$12450.00` en 1.245.000 pesos.
    """
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    match = _MONEY.search(raw)
    if not match:
        return None
    try:
        return float(match[1])
    except ValueError:
        return None


def group_id_to_int(group_id: str | None) -> int | None:
    """"catv00001255" -> 1255. None si no tiene la forma esperada."""
    if not group_id:
        return None
    match = _GROUP_ID.match(group_id.strip())
    if not match:
        return None
    return int(match[2])


def int_to_group_id(value: int) -> str:
    """1255 -> "catv00001255", para volver a pedirle la categoría a la API."""
    return f"{_GROUP_ID_PREFIX}{value:0{_GROUP_ID_DIGITS}d}"


def _price_entry(data: dict[str, Any], store_id: str) -> dict[str, Any] | None:
    """Entrada de precio de una sucursal, o None si esa sucursal no la publica."""
    for entry in data.get("price") or []:
        if isinstance(entry, dict) and str(entry.get("store") or "") == store_id:
            return entry
    return None


def resolve_price_entry(
    data: dict[str, Any], config: CotoConfig, store_id: str
) -> tuple[dict[str, Any] | None, PriceScope]:
    """Precio de la sucursal pedida, con caída a la de referencia.

    Devuelve además el nivel realmente alcanzado: si la sucursal pedida no
    publica precio para ese producto, lo que se devuelve es el nacional y hay
    que decirlo. Etiquetar el precio de referencia como si fuera el de tu
    sucursal es exactamente lo que `PriceScope` existe para evitar.
    """
    if store_id != config.default_store:
        entry = _price_entry(data, store_id)
        if entry is not None:
            return entry, PriceScope.STORE

    entry = _price_entry(data, config.default_store)
    if entry is not None:
        return entry, PriceScope.NATIONAL

    # Algunos productos no traen la sucursal de referencia; el precio a nivel
    # producto sigue siendo utilizable como nacional.
    fallback = data.get("product_list_price")
    if fallback is not None:
        return {"store": config.default_store, "listPrice": fallback}, PriceScope.NATIONAL
    return None, PriceScope.NATIONAL


def _shelf_price_cents(entry: dict[str, Any]) -> int | None:
    """Precio de góndola, en centavos: es `listPrice`.

    **No es `formatPrice`, aunque el nombre invite.** `formatPrice` es el precio
    por unidad de medida (ver `_unit_price_cents`); usarlo como precio de venta
    publica un aceite de 500 ml a $27.598 en lugar de $13.799 y un Fritolim de
    120 g a $40.833 en lugar de $4.900.

    Medido: el `listPrice` de la sucursal de referencia coincide con el
    `product_list_price` del producto en 245 de 250 casos.
    """
    return to_cents(entry.get("listPrice"))


def _unit_price_cents(
    entry: dict[str, Any], config: CotoConfig, price_cents: int
) -> int | None:
    """Precio por unidad de medida, en centavos: es `formatPrice`.

    Coto lo regala ya calculado, que es más de lo que da VTEX —ahí hay que
    derivarlo del `unitMultiplier`—. Es `listPrice` dividido por el tamaño del
    envase expresado en `product_format` (Litro o Kilo)::

        Aceite 500 ml:  13799 / 0,5   = 27598      (formatPrice = 27598)
        Aceite 1,5 L:    6530 / 1,5   =  4353,33   (formatPrice = 4353,34)
        Fritolim 120 g:  4900 / 0,120 = 40833,33   (formatPrice = 40833,33)

    Se valida contra la banda de `CotoConfig` porque la sucursal 133 lo publica
    roto (29,05 para una leche de 1 L de $2.495). Ahí se descarta el precio por
    unidad y se conserva el de góndola: tirar la oferta entera por un campo
    accesorio sería peor.
    """
    unit = to_cents(entry.get("formatPrice"))
    if unit is None or unit <= 0 or price_cents <= 0:
        return None
    ratio = unit / price_cents
    if not (config.min_unit_price_ratio <= ratio <= config.max_unit_price_ratio):
        logger.debug(
            "%s: formatPrice implausible en sucursal %s (%s vs %s)",
            config.slug,
            entry.get("store"),
            unit,
            price_cents,
        )
        return None
    return unit


def _taking_quantity(taking_text: str | None) -> int | None:
    """"Llevando 2" -> 2. None si la promo no exige cantidad."""
    if not taking_text:
        return None
    match = _TAKING_QUANTITY.search(taking_text)
    if not match:
        return None
    quantity = int(match[1])
    return quantity if quantity > 1 else None


def _parse_discount(
    raw: dict[str, Any], shelf_price_cents: int | None = None
) -> tuple[Promotion, float | None] | None:
    """Convierte una entrada de `discounts` en `(promoción, precio unitario)`.

    El segundo elemento es el precio que efectivamente se paga por **una**
    unidad, o None cuando la promo exige llevar más de una. Esa distinción es
    todo el punto de esta función: ver `parse_promotions`.

    La mecánica (`2x1`, `50% 2da`) la desarma `promo_text`, que es el mismo
    parser que usa VTEX: los carteles de Coto y los de Carrefour están escritos
    en el mismo idioma, y tener dos gramáticas distintas garantizaba que se
    fueran separando.
    """
    text = (raw.get("discountText") or "").strip()
    if not text:
        return None

    minimum = _taking_quantity(raw.get("takingText"))
    discount_price = parse_money(raw.get("discountPrice"))

    mechanics = parse_promo_text(text, unit_price_cents=shelf_price_cents)
    if mechanics is not None:
        promotion = Promotion(
            name=text,
            kind=mechanics.kind,
            # El porcentaje sólo se declara si aplica a la compra entera. En
            # "50% 2da" el 50 es sobre una unidad de dos, y publicarlo como 50%
            # duplicaría el ahorro real: va en `nth_unit_percent_off`.
            percent=None,
            minimum_quantity=minimum or mechanics.bundle_quantity,
            paid_quantity=mechanics.paid_quantity,
            nth_unit_percent_off=mechanics.nth_unit_percent_off,
            bundle_price_cents=mechanics.bundle_price_cents,
            effective_percent=mechanics.effective_percent,
            description=mechanics.describe() or None,
            requires_membership=membership_required(text),
        )
        return promotion, None

    # Descuento directo ("35%Dto", "+10%"): no exige llevar más de uno, así que
    # `discountPrice` sí es el precio de una unidad suelta.
    percent: float | None = None
    kind = PromotionKind.OTHER
    if match := _PERCENT.search(text):
        percent = float(match[1])
        kind = (
            PromotionKind.BUY_X_GET_Y if minimum else PromotionKind.PERCENT_DISCOUNT
        )

    promotion = Promotion(
        name=text,
        kind=kind,
        percent=percent if kind is PromotionKind.PERCENT_DISCOUNT else None,
        minimum_quantity=minimum,
        requires_membership=membership_required(text),
    )
    unit_price = None if minimum else discount_price
    return promotion, unit_price


def _payment_promotions(data: dict[str, Any]) -> tuple[Promotion, ...]:
    """Promos atadas a un medio de pago.

    Nunca tocan el precio: no todos las obtienen. Van a `promotions` para que el
    comparador de medios de pago las tenga en cuenta por separado.
    """
    promotions: list[Promotion] = []
    for raw in data.get("discounts_payment_methods") or []:
        if not isinstance(raw, dict):
            continue
        installments = str(raw.get("cantidadCuotas") or "").strip()
        price = parse_money(raw.get("precioCuota"))
        label = (raw.get("comentarios") or "").strip()
        if not label:
            label = f"Precio con tarjeta: ${price:.2f}" if price else "Precio con tarjeta"
            if installments and installments != "1":
                label = f"{installments} cuotas de ${price:.2f}" if price else label
        promotions.append(
            Promotion(
                name=label,
                kind=PromotionKind.PAYMENT_METHOD,
                requires_membership=membership_required(label),
            )
        )
    return tuple(promotions)


def parse_promotions(
    data: dict[str, Any], shelf_price_cents: int
) -> tuple[tuple[Promotion, ...], int | None]:
    """Promos del producto y, si la hay, el precio real de una unidad suelta.

    **La distinción que hace esta función es la que decide si el precio que
    mostramos es real.** Coto mezcla dos cosas en el mismo array `discounts`:

    * Descuento directo (`"25%Dto"`, sin `takingText`): se paga menos por una
      sola unidad. `discountPrice` *es* el precio, y `listPrice` pasa a ser el
      precio tachado.
    * Promo por cantidad (`"50% 2da"` o `"2x1"`, con `takingText: "Llevando 2"`):
      `discountPrice` es el promedio por unidad **llevando dos**. Aplicarlo a una
      unidad inventa un descuento que en la caja no existe, y como esta canasta
      compite contra Carrefour y Disco por el total más barato, ese descuento
      inventado le haría ganar a Coto una comparación que no gana.

    Devuelve `(promociones, precio unitario en centavos o None)`.
    """
    promotions: list[Promotion] = []
    unit_price_cents: int | None = None

    for raw in data.get("discounts") or []:
        if not isinstance(raw, dict):
            continue
        parsed = _parse_discount(raw, shelf_price_cents)
        if parsed is None:
            continue
        promotion, unit_price = parsed
        promotions.append(promotion)

        candidate = to_cents(unit_price)
        # Un "descuento" que no baja el precio es ruido, y uno que lo baja a
        # cero o lo sube es dato roto.
        if candidate is not None and 0 < candidate < shelf_price_cents:
            unit_price_cents = (
                candidate if unit_price_cents is None else min(unit_price_cents, candidate)
            )

    return (*promotions, *_payment_promotions(data)), unit_price_cents


def _image_url(data: dict[str, Any]) -> str | None:
    for key in ("image_url", "product_large_image_url", "product_medium_image_url"):
        url = data.get(key)
        if isinstance(url, str) and url.strip():
            return url.strip()
    return None


def _category_path(data: dict[str, Any]) -> tuple[str, ...]:
    """Camino de categorías más profundo que declare el producto.

    Cada entrada de `groups` trae su `path_list` (raíz -> padre) y su propio
    `display_name`; el camino completo es la concatenación. Se elige el más
    largo, que es el más específico.
    """
    best: tuple[str, ...] = ()
    for group in data.get("groups") or []:
        if not isinstance(group, dict):
            continue
        names = [
            entry.get("display_name")
            for entry in group.get("path_list") or []
            if isinstance(entry, dict) and entry.get("display_name")
        ]
        if group.get("display_name"):
            names.append(group["display_name"])
        # "Categorias" es la raíz sintética del árbol, no una categoría.
        path = tuple(str(n) for n in names if n and n != "Categorias")
        if len(path) > len(best):
            best = path
    return best


def _link(data: dict[str, Any], config: CotoConfig) -> str | None:
    url = data.get("url")
    if not isinstance(url, str) or not url.strip():
        return None
    path = url.strip()
    if path.startswith("http"):
        return path
    return f"{config.site_base_url}/sitios/cdigi/productos/{path.lstrip('/')}"


def _available(data: dict[str, Any], store_id: str, has_price: bool) -> bool:
    """Si la sucursal vende el producto.

    `store_availability` es la lista de sucursales que lo tienen. Cuando el
    campo no viene, se cae a "tiene precio", que es lo único que se puede
    afirmar.
    """
    availability = data.get("store_availability")
    if isinstance(availability, list) and availability:
        return store_id in {str(s) for s in availability}
    return has_price


def map_product_offer(
    raw: dict[str, Any],
    config: CotoConfig,
    *,
    captured_at: datetime,
    store: Store | None = None,
    store_id: str | None = None,
) -> ProductOffer | None:
    """Mapea un resultado de Constructor.io a una oferta.

    Devuelve None cuando el resultado no sirve para comparar precios: sin id,
    sin precio, o con un precio de cero. Es el mismo criterio que en VTEX —una
    oferta sin precio no es una oferta— y el llamador simplemente la saltea.

    A diferencia de VTEX no hay fan-out: en Constructor.io un resultado es un
    SKU, no un producto con N items y N sellers.
    """
    data = raw.get("data")
    if not isinstance(data, dict):
        return None

    product_id = str(data.get("id") or "").strip()
    if not product_id:
        return None

    resolved_store = store_id or config.resolve_price_scope(store)[0]
    entry, scope = resolve_price_entry(data, config, resolved_store)
    if entry is None:
        return None

    price_cents = _shelf_price_cents(entry)
    if price_cents is None or price_cents <= 0:
        return None

    promotions, discounted_cents = parse_promotions(data, price_cents)

    # Un descuento directo cambia el precio y manda el de góndola al tachado.
    reference_cents: int | None = None
    if discounted_cents is not None:
        reference_cents = price_cents
        price_cents = discounted_cents

    priced_store = str(entry.get("store") or resolved_store)
    name = (
        data.get("sku_display_name")
        or data.get("sku_description")
        or raw.get("value")
        or ""
    )

    product = Product(
        chain_slug=config.slug,
        product_id=product_id,
        sku_id=str(data.get("sku_id") or data.get("sku_plu") or product_id),
        name=str(name).strip(),
        brand=(str(data.get("product_brand")).strip() or None)
        if data.get("product_brand")
        else None,
        ean=normalize_ean(data.get("product_main_ean")),
        category_path=_category_path(data),
        image_url=_image_url(data),
        link=_link(data, config),
        measurement_unit=data.get("product_unit_of_measure") or None,
        unit_multiplier=_unit_multiplier(data),
    )

    offer = Offer(
        price_cents=price_cents,
        reference_price_cents=reference_cents,
        available=_available(data, priced_store, has_price=True),
        # Constructor.io no publica stock, sólo si la sucursal lo vende.
        available_quantity=0,
        seller_id=priced_store,
        price_scope=scope,
        # Sólo se nombra la sucursal si el precio es realmente suyo.
        store_key=store.key if store is not None and scope is PriceScope.STORE else None,
        price_per_unit_cents=_unit_price_cents(entry, config, price_cents),
        captured_at=captured_at,
        promotions=promotions,
    )
    return ProductOffer(product=product, offer=offer)


def _unit_multiplier(data: dict[str, Any]) -> float:
    try:
        multiplier = float(data.get("product_format_quantity") or 1)
    except (TypeError, ValueError):
        return 1.0
    return multiplier if multiplier > 0 else 1.0


def map_product_offers(
    results: list[dict[str, Any]],
    config: CotoConfig,
    *,
    captured_at: datetime,
    store: Store | None = None,
    store_id: str | None = None,
    drop_unmatched: bool = False,
) -> list[ProductOffer]:
    """Mapea una página de resultados, salteando lo que no sirve.

    Con `drop_unmatched` descarta los resultados sin `matched_terms`, que es como
    Constructor.io marca lo que devolvió por similitud semántica al no encontrar
    match léxico. Ver `CotoConfig.drop_unmatched_results`.
    """
    offers: list[ProductOffer] = []
    for raw in results:
        if not isinstance(raw, dict):
            continue
        if drop_unmatched and not raw.get("matched_terms"):
            continue
        offer = map_product_offer(
            raw, config, captured_at=captured_at, store=store, store_id=store_id
        )
        if offer is not None:
            offers.append(offer)
    return offers


def map_stores(
    results: list[dict[str, Any]], config: CotoConfig, postal_code: str | None = None
) -> list[Store]:
    """Deduce las sucursales a partir de los precios que publica el catálogo.

    Constructor.io no tiene endpoint de sucursales: lo único observable son los
    ids que aparecen en `data.price`. Alcanza para pedir el precio de una
    sucursal concreta, pero **no da ni nombre ni dirección ni código postal**, así
    que no sirve para resolver "qué sucursal me queda cerca". Por eso el
    proveedor no las expone en `find_stores`; ver el docstring de ahí.
    """
    seen: dict[str, None] = {}
    for raw in results:
        data = raw.get("data") if isinstance(raw, dict) else None
        if not isinstance(data, dict):
            continue
        for entry in data.get("price") or []:
            if isinstance(entry, dict) and (store_id := entry.get("store")):
                seen.setdefault(str(store_id), None)
    return [
        Store(
            chain_slug=config.slug,
            external_id=store_id,
            name=f"Sucursal {store_id}",
            postal_code=postal_code,
        )
        for store_id in sorted(seen)
    ]


def map_category_tree(
    groups: list[dict[str, Any]], _parent_path: tuple[int, ...] = ()
) -> tuple[CategoryNode, ...]:
    """Arma el árbol de categorías desde la faceta `groups`.

    La raíz que devuelve Constructor.io es un nodo sintético ("Categorias", con
    `group_id` no numérico); se atraviesa sin representarlo, para que el árbol
    empiece en las categorías reales.
    """
    nodes: list[CategoryNode] = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        children_raw = group.get("children") or []
        node_id = group_id_to_int(group.get("group_id"))
        if node_id is None:
            # Raíz sintética: se salta el nodo pero no sus hijos.
            nodes.extend(map_category_tree(children_raw, _parent_path))
            continue
        id_path = (*_parent_path, node_id)
        nodes.append(
            CategoryNode(
                id=node_id,
                name=str(group.get("display_name") or ""),
                id_path=id_path,
                children=map_category_tree(children_raw, id_path),
            )
        )
    return tuple(nodes)
