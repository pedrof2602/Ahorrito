"""Contrato que cumple todo proveedor de precios.

Es un `Protocol` y no una ABC a propósito: Coto y Día van a ser scrapers de HTML
que no comparten una línea de implementación con VTEX. Heredar de una base común
les daría una cadena de herencia que no quieren, y un Protocol deja que los tests
pasen un doble trivial sin ceremonia de registro.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Protocol, runtime_checkable

from app.models.catalog import (
    BasketSimulation,
    CategoryNode,
    Chain,
    ProductOffer,
    Store,
)


@runtime_checkable
class PriceProvider(Protocol):
    """Fuente de precios de una cadena."""

    @property
    def chain(self) -> Chain: ...

    async def find_stores(self, postal_code: str) -> list[Store]:
        """Sucursales que sirven a un código postal.

        Lista vacía si la cadena no expone regionalización: es una respuesta
        legítima, no un error.
        """
        ...

    async def search(
        self,
        term: str,
        *,
        store: Store | None = None,
        sales_channel: int | None = None,
        limit: int = 50,
    ) -> list[ProductOffer]:
        """Busca al nivel de precio más específico que la cadena permita.

        `store` y `sales_channel` son pedidos, no garantías: cada cadena resuelve
        lo que puede y declara el nivel alcanzado en `Offer.price_scope`. Una
        cadena que no soporta lo pedido cae a su precio nacional en lugar de
        fallar.
        """
        ...

    def iter_category(
        self,
        category: CategoryNode,
        *,
        store: Store | None = None,
        sales_channel: int | None = None,
    ) -> AsyncIterator[ProductOffer]: ...

    async def category_tree(self, depth: int = 3) -> tuple[CategoryNode, ...]: ...

    async def verify_prices(
        self,
        sku_quantities: Sequence[tuple[str, int]],
        *,
        store: Store | None = None,
        sales_channel: int | None = None,
    ) -> list[ProductOffer]:
        """Precio autoritativo de una canasta, confirmado contra checkout."""
        ...

    async def simulate_basket(
        self,
        sku_quantities: Sequence[tuple[str, int]],
        *,
        store: Store | None = None,
        sales_channel: int | None = None,
        card_bin: str | None = None,
    ) -> BasketSimulation | None:
        """Total de la canasta según el motor de promociones de la cadena.

        `card_bin` pide el total *con esa tarjeta*. Es la única forma honesta de
        saberlo: las promos no se acumulan y la cadena decide cuál gana en cada
        ítem, así que aplicarle nosotros el porcentaje del teaser al total
        inventa un ahorro que no existe.

        Devuelve `None` cuando la cadena no tiene un checkout consultable —Disco
        rechaza su propio catálogo con `ORD027`—. Es una respuesta legítima, no
        un error: el llamador cae al precio de catálogo y lo declara como tal.
        """
        ...

    async def aclose(self) -> None: ...
