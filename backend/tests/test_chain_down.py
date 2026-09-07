"""Qué pasa cuando un supermercado se cae.

La resiliencia del fan-out —`search_all`, la caché stale— ya está probada en otro
lado. Lo que se cubre acá son los caminos que la rodeaban: el localizador de
sucursales, que corría sin guarda y tiraba la request entera antes de llegar al
fan-out; el checkout, que devolvía un 500 sin decir de quién era la culpa; y la
red de contención que traduce lo que se escape a un status que señale hacia
afuera.

Cada test tiene la misma forma: una cadena rota, otra sana, y la pregunta de si
la sana sigue llegando.
"""

from __future__ import annotations

import pytest
from fastapi import Request

from app.main import _provider_error, _provider_unavailable
from app.models.basket import ChainErrorOut, first_per_chain
from app.models.catalog import Chain, Offer, PriceScope, Product, ProductOffer, Store
from app.services.http import (
    ProviderBlocked,
    ProviderRateLimited,
    ProviderUnavailable,
)

CARREFOUR = Chain(
    slug="carrefour-ar",
    display_name="Carrefour",
    supports_store_prices=True,
    sales_channels=(1,),
    default_sales_channel=1,
)

DISCO = Chain(
    slug="disco-ar",
    display_name="Disco",
    supports_store_prices=True,
    sales_channels=(1,),
    default_sales_channel=1,
)


def _offer(chain: Chain, price_cents: int) -> ProductOffer:
    return ProductOffer(
        product=Product(
            chain_slug=chain.slug,
            product_id=f"p-{chain.slug}",
            sku_id=f"s-{chain.slug}",
            name="Leche entera 1L",
            ean="7791720023969",
        ),
        offer=Offer(
            price_cents=price_cents,
            available=True,
            captured_at="2026-08-31T12:00:00+00:00",
            price_scope=PriceScope.NATIONAL,
        ),
    )


class FakeProvider:
    """Cadena de mentira que se rompe donde se le pida.

    Los tres fallos van por separado a propósito: una cadena puede tener el
    localizador caído y el catálogo sano —es el caso interesante— y un fake con
    un solo interruptor no lo podría representar.
    """

    def __init__(
        self,
        chain: Chain,
        *,
        stores: list[Store] | None = None,
        find_stores_fails: Exception | None = None,
        search_fails: Exception | None = None,
        verify_fails: Exception | None = None,
    ) -> None:
        self.chain = chain
        self._stores = stores or []
        self._find_stores_fails = find_stores_fails
        self._search_fails = search_fails
        self._verify_fails = verify_fails
        self.searched_with: list[Store | None] = []

    async def find_stores(self, postal_code: str) -> list[Store]:
        if self._find_stores_fails is not None:
            raise self._find_stores_fails
        return self._stores

    async def search(self, term, *, store=None, sales_channel=None, limit=50):
        self.searched_with.append(store)
        if self._search_fails is not None:
            raise self._search_fails
        return [_offer(self.chain, 250000)]

    async def verify_prices(self, sku_quantities, *, store=None, sales_channel=None):
        if self._verify_fails is not None:
            raise self._verify_fails
        return [_offer(self.chain, 250000)]

    async def simulate_basket(self, *args, **kwargs):
        return None

    async def aclose(self) -> None:
        return None


def _store(chain: Chain) -> Store:
    return Store(
        chain_slug=chain.slug,
        external_id=f"{chain.slug}-0026",
        name="Hiper Warnes",
        postal_code="1425",
    )


@pytest.fixture
def registry_of():
    """Instala proveedores de mentira en la app y devuelve el registry.

    Se sobrescribe la dependencia `get_registry` y no `app.state`: el `client` de
    `conftest` corre sin lifespan, así que `app.state.registry` no existe.
    """
    from app.api.v1.prices import get_registry
    from app.main import app
    from app.services.providers.registry import ProviderRegistry

    def install(*providers: FakeProvider) -> ProviderRegistry:
        registry = ProviderRegistry(providers)
        app.dependency_overrides[get_registry] = lambda: registry
        return registry

    yield install
    app.dependency_overrides.clear()


# --- el localizador de sucursales ------------------------------------------


@pytest.mark.asyncio
async def test_localizador_caido_no_tira_la_busqueda(client, registry_of):
    """Antes, el fallo subía sin guarda y devolvía un 500 con las manos vacías."""
    registry_of(
        FakeProvider(CARREFOUR, find_stores_fails=ProviderUnavailable("timeout")),
        FakeProvider(DISCO, stores=[_store(DISCO)]),
    )

    response = await client.get(
        "/api/v1/search", params={"q": "leche", "postal_code": "1425"}
    )

    assert response.status_code == 200
    body = response.json()
    # Las dos cadenas contestaron precios: la rota lo hizo sin sucursal.
    assert body["count"] == 2
    assert body["partial"] is True
    assert [e["chain_slug"] for e in body["errors"]] == ["carrefour-ar"]


@pytest.mark.asyncio
async def test_la_cadena_sin_sucursal_igual_se_consulta(client, registry_of):
    """No poder ubicar la sucursal degrada a precio nacional, no cancela.

    Es la mitad del contrato que hace que el fallo se informe igual: los precios
    de esa cadena están, pero no son los del barrio que se pidió.
    """
    carrefour = FakeProvider(
        CARREFOUR, find_stores_fails=ProviderUnavailable("timeout")
    )
    disco = FakeProvider(DISCO, stores=[_store(DISCO)])
    registry_of(carrefour, disco)

    await client.get(
        "/api/v1/search", params={"q": "leche", "postal_code": "1425"}
    )

    assert carrefour.searched_with == [None]
    assert disco.searched_with == [_store(DISCO)]


@pytest.mark.asyncio
async def test_stores_devuelve_las_de_las_cadenas_que_contestaron(client, registry_of):
    registry_of(
        FakeProvider(CARREFOUR, find_stores_fails=ProviderUnavailable("timeout")),
        FakeProvider(DISCO, stores=[_store(DISCO)]),
    )

    response = await client.get("/api/v1/stores", params={"postal_code": "1425"})

    assert response.status_code == 200
    assert [s["chain_slug"] for s in response.json()] == ["disco-ar"]


@pytest.mark.asyncio
async def test_stores_no_dice_que_no_hay_si_no_pudo_preguntar(client, registry_of):
    """Lista vacía por fallo ≠ lista vacía porque no hay sucursales cerca.

    Devolver `[]` acá sería la respuesta que hace cambiar de barrio a alguien que
    tenía un Carrefour a tres cuadras.
    """
    registry_of(
        FakeProvider(CARREFOUR, find_stores_fails=ProviderUnavailable("timeout")),
        FakeProvider(DISCO, find_stores_fails=ProviderUnavailable("timeout")),
    )

    response = await client.get("/api/v1/stores", params={"postal_code": "1425"})

    assert response.status_code == 502
    assert "carrefour-ar" in response.json()["detail"]


@pytest.mark.asyncio
async def test_sin_codigo_postal_no_se_toca_el_localizador(client, registry_of):
    """Una cadena con el localizador roto no molesta si nadie preguntó."""
    registry_of(
        FakeProvider(CARREFOUR, find_stores_fails=ProviderUnavailable("timeout"))
    )

    response = await client.get("/api/v1/search", params={"q": "leche"})

    assert response.status_code == 200
    assert response.json()["errors"] == []


# --- el checkout ------------------------------------------------------------


@pytest.mark.asyncio
async def test_checkout_caido_da_503_y_no_500(client, registry_of):
    registry_of(FakeProvider(CARREFOUR, verify_fails=ProviderUnavailable("timeout")))

    response = await client.post(
        "/api/v1/basket/verify",
        json={"chain_slug": "carrefour-ar", "items": [{"sku_id": "s1", "quantity": 1}]},
    )

    assert response.status_code == 503
    assert "Carrefour" in response.json()["detail"]


@pytest.mark.asyncio
async def test_checkout_que_responde_cualquier_cosa_da_502(client, registry_of):
    """Reintentar no lo arregla: cambió su API o nos frenó un challenge de bot."""
    registry_of(FakeProvider(CARREFOUR, verify_fails=ProviderBlocked("no era JSON")))

    response = await client.post(
        "/api/v1/basket/verify",
        json={"chain_slug": "carrefour-ar", "items": [{"sku_id": "s1", "quantity": 1}]},
    )

    assert response.status_code == 502


# --- la red de contención ---------------------------------------------------


def _request() -> Request:
    return Request(
        {"type": "http", "method": "GET", "path": "/api/v1/search", "headers": []}
    )


@pytest.mark.asyncio
async def test_lo_que_se_escapa_sale_como_503_reintentable():
    response = await _provider_unavailable(_request(), ProviderUnavailable("timeout"))
    assert response.status_code == 503


@pytest.mark.asyncio
async def test_si_la_cadena_dijo_cuanto_esperar_se_le_pasa_al_cliente():
    response = await _provider_unavailable(
        _request(), ProviderRateLimited("Coto limitó la tasa", retry_after=30)
    )
    assert response.status_code == 503
    assert response.headers["retry-after"] == "30"


@pytest.mark.asyncio
async def test_una_respuesta_inutilizable_sale_como_502():
    response = await _provider_error(_request(), ProviderBlocked("no era JSON"))
    assert response.status_code == 502


# --- un error por cadena ----------------------------------------------------


def test_una_cadena_caida_se_cuenta_una_sola_vez():
    """Una canasta de quince líneas devolvía quince veces el mismo fallo."""
    errors = [
        ChainErrorOut(chain_slug="coto-ar", message="falló 'leche'"),
        ChainErrorOut(chain_slug="disco-ar", message="falló 'pan'"),
        ChainErrorOut(chain_slug="coto-ar", message="falló 'pan'"),
        ChainErrorOut(chain_slug="coto-ar", message="checkout no disponible"),
    ]

    kept = first_per_chain(errors)

    assert [e.chain_slug for e in kept] == ["coto-ar", "disco-ar"]
    # El primero, que es la causa: si la búsqueda ya falló, que además no
    # responda el checkout es consecuencia y no una noticia aparte.
    assert kept[0].message == "falló 'leche'"
