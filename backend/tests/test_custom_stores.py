"""Sucursales cargadas a mano y búsqueda de direcciones, por HTTP."""

from __future__ import annotations

import httpx
import pytest
import pytest_asyncio
import respx

from app.core.config import settings
from app.db.repositories import ChainRepository
from app.models.catalog import Chain
from app.services.locations import georef
from tests.conftest import TEST_PASSWORD


class _FakeProvider:
    def __init__(self, slug: str) -> None:
        self.chain = Chain(slug=slug, display_name=slug, supports_store_prices=False)


class _FakeRegistry:
    """Las cadenas que la app sabe cotizar, sin levantar clientes HTTP.

    El registry real construye un cliente contra cada supermercado; acá lo único
    que se consulta es la lista de slugs habilitados.
    """

    def all(self):
        return [_FakeProvider("carrefour-ar"), _FakeProvider("coto-ar")]


@pytest_asyncio.fixture
async def client(session, user, monkeypatch):
    """Como el `client` de `conftest`, más el registry falso.

    No reusa aquel porque estos endpoints necesitan además que la lista de
    cadenas comparables no levante clientes HTTP contra los supermercados.
    """
    monkeypatch.setattr(settings, "COOKIE_SECURE", False)

    from app.api.v1.locations import get_registry
    from app.core.db import get_db
    from app.main import app

    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[get_registry] = lambda: _FakeRegistry()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        await c.post(
            "/api/v1/auth/login",
            json={"email": user.email, "password": TEST_PASSWORD},
        )
        yield c
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def coto(session):
    chain = await ChainRepository(session).upsert(
        Chain(slug="coto-ar", display_name="Coto", supports_store_prices=False)
    )
    await session.commit()
    return chain


COTO_STORE = {
    "chain_slug": "coto-ar",
    "name": "Coto de casa",
    "latitude": -34.6056,
    "longitude": -58.4135,
    "address": "Gallo 250",
}


@pytest.mark.asyncio
async def test_alta_lectura_y_baja(client, coto):
    created = await client.post("/api/v1/custom-stores", json=COTO_STORE)
    assert created.status_code == 201, created.text
    store_id = created.json()["id"]
    assert created.json()["chain_name"] == "Coto"

    listed = await client.get("/api/v1/custom-stores")
    assert [s["name"] for s in listed.json()] == ["Coto de casa"]

    assert (await client.delete(f"/api/v1/custom-stores/{store_id}")).status_code == 204
    assert (await client.get("/api/v1/custom-stores")).json() == []


@pytest.mark.asyncio
async def test_solo_se_pueden_cargar_cadenas_que_la_app_compara(client, coto):
    """Es lo que sostiene que todo marker del mapa tenga un total al lado.

    Un Chango Más cargado a mano dibujaría un marker sin precio junto a otros con
    precio, y dos markers que se ven igual y significan distinto confunden más de
    lo que ayudan.
    """
    response = await client.post(
        "/api/v1/custom-stores", json={**COTO_STORE, "chain_slug": "changomas-ar"}
    )
    assert response.status_code == 422
    assert "carrefour-ar" in response.json()["detail"]


@pytest.mark.asyncio
async def test_no_se_guarda_una_sucursal_sin_ubicar(client, coto):
    """(0, 0) es el Golfo de Guinea: un formulario a medio llenar, no un local.

    Sin este corte `fitBounds` estira el mapa hasta África y las sucursales
    reales quedan amontonadas en un punto.
    """
    response = await client.post(
        "/api/v1/custom-stores", json={**COTO_STORE, "latitude": 0, "longitude": 0}
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_no_se_repite_el_nombre_dentro_de_la_misma_cadena(client, coto):
    assert (await client.post("/api/v1/custom-stores", json=COTO_STORE)).status_code == 201
    repeated = await client.post("/api/v1/custom-stores", json=COTO_STORE)
    assert repeated.status_code == 409
    assert "Coto de casa" in repeated.json()["detail"]


@pytest.mark.asyncio
async def test_editar_mueve_el_marker(client, coto):
    created = await client.post("/api/v1/custom-stores", json=COTO_STORE)
    store_id = created.json()["id"]

    moved = await client.patch(
        f"/api/v1/custom-stores/{store_id}", json={"latitude": -34.61}
    )
    assert moved.status_code == 200
    assert moved.json()["latitude"] == pytest.approx(-34.61)
    # Lo que no se mandó no se toca: un PATCH que borrara la dirección al mover
    # el punto haría perder datos al corregir la ubicación.
    assert moved.json()["address"] == "Gallo 250"


@pytest.mark.asyncio
async def test_endpoints_inexistentes_dan_404(client, coto):
    assert (await client.delete("/api/v1/custom-stores/999")).status_code == 404
    assert (await client.patch("/api/v1/custom-stores/999", json={})).status_code == 404


# ----------------------------------------------------------------- geocodificar


@respx.mock
@pytest.mark.asyncio
async def test_buscar_direccion_devuelve_las_opciones(client):
    respx.get(url__startswith=f"{georef.BASE_URL}{georef.SEARCH_PATH}").mock(
        return_value=httpx.Response(
            200,
            json={
                "cantidad": 1,
                "direcciones": [
                    {
                        "nomenclatura": "GALLO 250, Comuna 3, CABA",
                        "ubicacion": {"lat": -34.6056, "lon": -58.4135},
                        "departamento": {"nombre": "Comuna 3"},
                        "provincia": {"nombre": "Ciudad Autónoma de Buenos Aires"},
                    }
                ],
            },
        )
    )
    response = await client.get("/api/v1/geocode", params={"address": "Gallo 250"})

    assert response.status_code == 200
    assert response.json()["candidates"][0]["city"] == "Comuna 3"


@respx.mock
@pytest.mark.asyncio
async def test_si_el_geocodificador_no_contesta_se_dice_y_se_ofrece_la_salida(client):
    """El caso que de verdad pasa: la red del usuario no llega al servicio.

    502 y no 500, porque el que falló es de afuera, y el mensaje tiene que
    mandar a marcar el punto en el mapa en vez de dejar a alguien creyendo que
    su dirección no existe.
    """
    respx.get(url__startswith=f"{georef.BASE_URL}{georef.SEARCH_PATH}").mock(
        side_effect=httpx.ConnectTimeout("sin ruta al host")
    )
    response = await client.get("/api/v1/geocode", params={"address": "Gallo 250"})

    assert response.status_code == 502
    assert "en el mapa" in response.json()["detail"]


@respx.mock
@pytest.mark.asyncio
async def test_una_direccion_que_no_existe_no_es_un_error(client):
    respx.get(url__startswith=f"{georef.BASE_URL}{georef.SEARCH_PATH}").mock(
        return_value=httpx.Response(200, json={"cantidad": 0, "direcciones": []})
    )
    response = await client.get("/api/v1/geocode", params={"address": "Calle Falsa 123"})

    assert response.status_code == 200
    assert response.json()["candidates"] == []
    assert "mapa" in response.json()["note"]
