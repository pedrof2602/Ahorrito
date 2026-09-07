"""Modelos canónicos del catálogo.

Independientes de VTEX: cualquier proveedor (VTEX, Coto, Día) mapea a estos.
El dinero viaja como enteros en centavos para evitar drift de punto flotante
en el histórico de precios.
"""

from collections.abc import Iterable
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class PriceScope(StrEnum):
    """Qué tan específico es un precio, y por lo tanto cuánto vale.

    Los tres niveles son la cadena de resolución: se intenta el más específico y
    se cae al siguiente. Distinguirlos es obligatorio —mostrar un precio nacional
    como si fuera el de tu sucursal es inventar una precisión que no tenemos—, y
    por eso el nivel alcanzado viaja en cada oferta en lugar de deducirse de si se
    pidió un código postal.
    """

    STORE = "store"
    """Precio atribuible a una sucursal concreta.

    Requiere que la cadena permita saber qué canal de venta usa esa sucursal.
    Hoy ninguna de las soportadas lo expone: `/regions` de Carrefour identifica
    la sucursal pero no su canal, y la simulación devuelve el mismo precio para
    todos los sellers."""

    CHANNEL = "channel"
    """Precio de un canal de venta elegido (`sc` en VTEX), no de una sucursal.

    Es una diferencia real y grande —Carrefour publica la Leche Protein a $2249
    en el canal 1 y a $2999 en el 3—, pero corresponde a un formato de tienda,
    no a la sucursal de tu barrio."""

    NATIONAL = "national"
    """Canal por defecto de la cadena. Es el piso al que se cae siempre."""


class PromotionKind(StrEnum):
    PERCENT_DISCOUNT = "percent_discount"
    NOMINAL_DISCOUNT = "nominal_discount"
    BUY_X_GET_Y = "buy_x_get_y"
    PAYMENT_METHOD = "payment_method"  # ej. "Tarjeta Carrefour 15%"
    OTHER = "other"


class Chain(BaseModel):
    """Cadena de supermercados (Carrefour, Disco...)."""

    model_config = ConfigDict(frozen=True)

    slug: str = Field(..., examples=["carrefour-ar"])
    display_name: str = Field(..., examples=["Carrefour"])
    supports_store_prices: bool = Field(
        ...,
        description="Si la cadena permite obtener precios por sucursal.",
    )
    sales_channels: tuple[int, ...] = Field(
        (),
        description=(
            "Canales de venta consultables. Publican precios distintos para el "
            "mismo producto, así que son la palanca real para acercarse al precio "
            "de tu sucursal. Vacío = la cadena solo tiene precio nacional."
        ),
        examples=[[1, 3, 5]],
    )
    default_sales_channel: int | None = Field(
        None, description="Canal usado cuando no se pide ninguno."
    )


class Store(BaseModel):
    """Sucursal o canal de venta de una cadena."""

    model_config = ConfigDict(frozen=True)

    chain_slug: str = Field(..., examples=["carrefour-ar"])
    external_id: str = Field(
        ...,
        description="Identificador en la cadena (sellerId de VTEX, o 'sc:3').",
        examples=["carrefourar0026"],
    )
    name: str = Field(..., examples=["Hiper Warnes"])
    sales_channel: int | None = Field(
        None, description="Sales channel de VTEX, si la cadena lo usa."
    )
    region_id: str | None = Field(
        None, description="regionId de VTEX asociado al código postal."
    )
    postal_code: str | None = None

    # --- ubicación: vacía mientras nadie la haya cargado --------------------
    latitude: float | None = None
    longitude: float | None = None
    address: str | None = Field(None, examples=["Warnes 2001"])
    city: str | None = Field(None, examples=["CABA"])
    province: str | None = None
    location_source: str | None = Field(
        None,
        description=(
            "`api` si la coordenada la publicó la cadena, `geocoded` si salió de "
            "geocodificar la dirección. La segunda es aproximada."
        ),
        examples=["api"],
    )

    @property
    def key(self) -> str:
        return f"{self.chain_slug}:{self.external_id}"

    @property
    def is_locatable(self) -> bool:
        """Si se puede poner en un mapa.

        No toda sucursal lo es: en las cadenas que regionalizan por canal de
        venta, la "sucursal" es un canal y no un lugar.
        """
        return self.latitude is not None and self.longitude is not None


class Promotion(BaseModel):
    """Promoción aplicable a una oferta."""

    model_config = ConfigDict(frozen=True)

    name: str = Field(..., examples=["Tarjeta Carrefour 15%"])
    kind: PromotionKind = PromotionKind.OTHER
    percent: float | None = Field(
        None,
        description=(
            "Descuento porcentual sobre la compra entera. **None en las promos "
            "por cantidad**: en un '2do al 70%' el 70 no se paga sobre todo, y "
            "publicarlo acá duplicaría el ahorro. Ese caso va en "
            "`nth_unit_percent_off` y su ahorro real en `effective_percent`."
        ),
    )
    minimum_quantity: int | None = Field(
        None, description="Unidades que hay que llevar para que la promo aplique."
    )

    # --- Mecánica de las promos por cantidad -------------------------------
    # El título de góndola ("2x1", "2do al 70%", "2x$2500") es publicidad, no
    # datos. Estos campos son ese título ya desarmado, para que la interfaz
    # pueda explicar qué te llevás sin volver a parsear el string.

    paid_quantity: int | None = Field(
        None,
        description="Unidades que se pagan en una NxM. En un '3x2' es 2.",
        examples=[2],
    )
    nth_unit_percent_off: float | None = Field(
        None,
        description=(
            "Descuento sobre la unidad que la promo bonifica. En un "
            "'2do al 70%' es 70 — el descuento de esa unidad, no el de la compra."
        ),
        examples=[70],
    )
    bundle_price_cents: int | None = Field(
        None,
        description="Precio total fijo del combo. En un '2x$2500' son 250000.",
    )
    effective_percent: float | None = Field(
        None,
        description=(
            "Ahorro real sobre el combo completo, y la única cifra comparable "
            "entre promos de formas distintas: un '2do al 70%' rinde 35% y un "
            "'3x2' rinde 33,3%, aunque el cartel del primero diga 70."
        ),
        examples=[35],
    )
    description: str | None = Field(
        None,
        description=(
            "La promo explicada en castellano. Habla de lo que la promo ofrece, "
            "no de lo que vas a pagar: el checkout a veces aplica otra mejor y a "
            "veces ninguna."
        ),
        examples=["Llevando 2 pagás 1: la 2da unidad te la llevás gratis (50% de ahorro llevando 2)."],
    )
    requires_membership: tuple[str, ...] = Field(
        (),
        description=(
            "Tarjetas o comunidades de la cadena que habilitan la promo, cuando "
            "el título lo declara. Son alternativas: alcanza con cualquiera. "
            "Vacío significa que el título no lo aclara, no que sea para "
            "cualquiera."
        ),
        examples=[["Mi Carrefour"]],
    )


class Product(BaseModel):
    """Producto tal como lo publica una cadena."""

    model_config = ConfigDict(frozen=True)

    chain_slug: str
    product_id: str = Field(..., description="productId en la cadena.")
    sku_id: str = Field(..., description="itemId del SKU en la cadena.")
    name: str
    brand: str | None = None
    ean: str | None = Field(None, description="GTIN normalizado a 13 dígitos.")
    category_path: tuple[str, ...] = ()
    image_url: str | None = None
    link: str | None = None
    measurement_unit: str | None = Field(None, examples=["un", "kg"])
    unit_multiplier: float = 1.0


class Offer(BaseModel):
    """Precio y disponibilidad de un SKU en un momento y ámbito dados."""

    model_config = ConfigDict(frozen=True)

    price_cents: int = Field(..., ge=0, description="Precio de venta en centavos.")
    reference_price_cents: int | None = Field(
        None,
        ge=0,
        description=(
            "Precio tachado / sin descuento. None cuando la cadena publica un "
            "ListPrice inconsistente (Disco devuelve valores absurdos)."
        ),
    )
    available: bool = True
    available_quantity: int = 0
    seller_id: str | None = None
    price_scope: PriceScope = PriceScope.NATIONAL
    store_key: str | None = Field(
        None, description="Store.key cuando price_scope es STORE."
    )
    price_per_unit_cents: int | None = Field(
        None, description="Precio por unidad de medida, para comparar 1L vs 900ml."
    )
    captured_at: datetime
    promotions: tuple[Promotion, ...] = ()

    @property
    def discount_percent(self) -> float | None:
        """Descuento real respecto del precio de referencia, si es confiable."""
        if not self.reference_price_cents or self.reference_price_cents <= self.price_cents:
            return None
        delta = self.reference_price_cents - self.price_cents
        return round(delta * 100 / self.reference_price_cents, 2)


class Freshness(BaseModel):
    """Cuándo se consultó realmente este precio, y si todavía se lo puede creer.

    Existe porque un precio servido de caché y uno recién traído se ven idénticos
    en la respuesta, y no valen lo mismo. Mismo criterio que `PriceScope`: el dato
    viaja con su propia calidad declarada en lugar de que la interfaz la deduzca.

    `stale` no significa "viejo": significa que se sirvió **vencido** porque la
    cadena no respondió. Un precio de cinco horas con TTL de seis no es stale, es
    un acierto de caché.
    """

    model_config = ConfigDict(frozen=True)

    fetched_at: datetime = Field(
        ..., description="Cuándo se le preguntó a la cadena por este precio."
    )
    age_seconds: int = Field(..., ge=0, description="Antigüedad al momento de responder.")
    from_cache: bool = Field(
        False, description="False si se trajo en vivo durante este pedido."
    )
    stale: bool = Field(
        False,
        description=(
            "El TTL venció y la cadena no respondió: es el último precio conocido "
            "y **puede estar desactualizado**. Mostralo marcado o no lo muestres."
        ),
    )

    @staticmethod
    def worst(items: Iterable["Freshness | None"]) -> "Freshness | None":
        """La frescura de un conjunto es la de su dato menos fresco.

        Mismo criterio que `_scope_of` para el `PriceScope` de un total: un
        carrito con dos precios de hace un minuto y uno de hace ocho horas está
        tan desactualizado como ese último. Anunciar el más fresco de los tres
        sería prometer una vigencia que el total no tiene.

        Un `None` en la lista —una oferta que no pasó por la caché— se ignora en
        vez de contar como fresca: no sabemos cuándo se trajo, y suponer que fue
        recién es justo el error que esto evita.
        """
        known = [f for f in items if f is not None]
        if not known:
            return None
        return max(known, key=lambda f: (f.stale, f.age_seconds))


class ProductOffer(BaseModel):
    """Un producto junto con su oferta vigente. Unidad de respuesta de la API."""

    model_config = ConfigDict(frozen=True)

    product: Product
    offer: Offer
    freshness: Freshness | None = Field(
        None,
        description=(
            "De dónde salió este precio. None cuando no pasó por la capa de "
            "caché (por ejemplo, un precio confirmado contra checkout)."
        ),
    )


class BasketSimulation(BaseModel):
    """Total de una canasta confirmado contra el checkout de la cadena.

    Es el número autoritativo: lo devuelve el motor de promociones de la propia
    cadena, con las promos por cantidad y por medio de pago ya resueltas. Existe
    como modelo aparte de `ProductOffer` porque lo que interesa acá es el total
    de la canasta, no el precio de cada SKU: las promos de VTEX **no se acumulan**
    —el motor elige una por ítem— así que el total no se puede reconstruir
    aplicándole un porcentaje a la suma de los precios de góndola.
    """

    model_config = ConfigDict(frozen=True)

    items_total_cents: int = Field(..., ge=0, description="Suma sin descuentos.")
    discount_cents: int = Field(
        0, ge=0, description="Descuento total aplicado, como magnitud positiva."
    )
    total_cents: int = Field(..., ge=0, description="Lo que realmente pagás.")
    promotions: tuple[Promotion, ...] = Field(
        (), description="Promos que el motor activó de verdad, no las que anuncia."
    )
    card_bin: str | None = Field(
        None, description="BIN con el que se simuló, si se pidió con tarjeta."
    )
    confirmed_sku_ids: frozenset[str] = frozenset()
    missing_sku_ids: frozenset[str] = Field(
        frozenset(),
        description=(
            "SKU que el checkout no confirmó. Con esto no vacío el total está "
            "incompleto y no se puede comparar contra otra cadena."
        ),
    )

    @property
    def complete(self) -> bool:
        """Si el checkout confirmó todos los SKU pedidos.

        Un total incompleto es más barato por estar incompleto, que es justo el
        número convincente y equivocado que hay que no mostrar.
        """
        return not self.missing_sku_ids


class CategoryNode(BaseModel):
    """Nodo del árbol de categorías de una cadena."""

    model_config = ConfigDict(frozen=True)

    id: int
    name: str
    # Path completo raíz->nodo; VTEX exige esto en fq=C:, no el id suelto.
    id_path: tuple[int, ...] = ()
    children: tuple["CategoryNode", ...] = ()

    @property
    def is_leaf(self) -> bool:
        return not self.children

    @property
    def fq(self) -> str:
        """Filtro de categoría para catalog_system: 'C:/3/4/5/'."""
        return "C:/" + "/".join(str(i) for i in self.id_path) + "/"
