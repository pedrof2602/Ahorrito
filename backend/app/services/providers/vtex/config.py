"""Configuración por tienda VTEX.

Esta es la costura entre lo genérico y lo específico: el cliente sabe hablar el
protocolo VTEX, y este archivo sabe en qué difiere cada inquilino. Los flags no
son preferencias, son comportamiento medido; cada uno lleva la evidencia al lado.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from app.models.catalog import PriceScope, Store


class RegionStrategy(StrEnum):
    """Cómo se obtienen precios por sucursal en una cadena."""

    SALES_CHANNEL = "sales_channel"
    """El parámetro `sc` cambia el precio (Carrefour: sc=1 -> $10000.90, sc=3 -> $13913.90)."""

    NONE = "none"
    """La cadena no expone regionalización pública. Precios nacionales."""


@dataclass(frozen=True, slots=True)
class VTEXStoreConfig:
    slug: str
    display_name: str
    base_url: str

    country: str = "ARG"
    region_strategy: RegionStrategy = RegionStrategy.NONE

    sales_channels: tuple[int, ...] = ()
    """Sales channels activos. Los inactivos devuelven HTTP 200 con el string
    `"sc is inactive"`, y los inexistentes `"sc not found"`."""

    default_sales_channel: int | None = None

    supports_region_discovery: bool = False
    """Si `/api/checkout/pub/regions` funciona. En Disco devuelve 500 (ORD021.6)."""

    supports_simulation: bool = True
    """Si `/api/checkout/pub/orderForms/simulation` devuelve precio autoritativo."""

    simulation_uses_store_seller: bool = False
    """Si el `sellerId` que devuelve `/regions` sirve para simular.

    **Medido en Carrefour: no solo no sirve, rompe.** Ya se sabía que la
    simulación devuelve el mismo precio para todos los sellers; lo nuevo es que
    mandar `carrefourar0026` en lugar del seller por defecto **apaga el motor de
    promociones**. La misma canasta con el mismo BIN pasa de $17.922,25 con
    "Tarjeta Carrefour 15%" aplicada a $21.085 sin ninguna promo — y responde
    HTTP 200, con todos los ítems confirmados y sin un solo mensaje de error.

    Es el peor modo de falla posible: un total completo, plausible y sin
    descuentos. Por eso el default es no usarlo, y una cadena que demuestre que
    su seller sí funciona lo activa explícitamente."""

    teaser_backing_fields: bool = False
    """Carrefour serializa `Teasers` con mangling de C#: `<Name>k__BackingField`."""

    excluded_category_ids: frozenset[int] = field(default_factory=frozenset)
    """Categorías basura del catálogo (Carrefour publica una 'Test Category')."""

    # Límites del protocolo, verificados contra las APIs reales.
    max_page_size: int = 50
    """`_to - _from + 1` no puede pasar de 50; 51 devuelve 400."""

    max_offset: int = 2500
    """`_from` no puede pasar de 2500; más arriba devuelve 400."""

    max_concurrency: int = 4
    min_interval_ms: int = 250
    timeout_s: float = 20.0

    @property
    def supports_store_prices(self) -> bool:
        return self.region_strategy is not RegionStrategy.NONE

    def resolve_price_scope(
        self, sales_channel: int | None = None, store: Store | None = None
    ) -> tuple[int | None, PriceScope]:
        """Elige el canal más específico posible y declara qué nivel se alcanzó.

        La cadena de resolución, de más a menos específico:

        1. El canal que la cadena atribuye a la sucursal (`store.sales_channel`)
           -> `STORE`.
        2. Un canal pedido explícitamente -> `CHANNEL`.
        3. El canal por defecto -> `NATIONAL`.

        **Nunca levanta excepción por un canal inválido.** Un `sc` que la cadena
        no tiene activo hace caer a nacional, no fallar: en una comparación entre
        supermercados, pedir un canal de Carrefour no puede dejarte sin los
        precios de Disco.
        """
        if self.region_strategy is RegionStrategy.NONE:
            return None, PriceScope.NATIONAL

        if store is not None and store.sales_channel in self.sales_channels:
            return store.sales_channel, PriceScope.STORE

        if sales_channel is not None and sales_channel in self.sales_channels:
            return sales_channel, PriceScope.CHANNEL

        return self.default_sales_channel, PriceScope.NATIONAL
