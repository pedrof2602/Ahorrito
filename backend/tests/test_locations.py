"""Ubicaciones de sucursales: descubrimiento, geocodificación y búsqueda."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
import respx
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.repositories import (
    AddressRepository,
    ChainRepository,
    CustomStoreRepository,
    GeocodeCacheRepository,
    StoreRepository,
)
from app.db.tables import Base
from app.models.catalog import Chain
from app.services.locations import (
    SOURCE_API,
    Location,
    coto_scrape,
    georef,
    haversine_km,
    normalize_name,
    vtex_pickup,
)
from app.services.locations.lookup import nearest_by_chain, resolve_center
from app.services.locations.nominatim import Geocoder, address_query

FIXTURES = Path(__file__).parent / "fixtures" / "locations"


def load_json(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def load_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# ------------------------------------------------------- VTEX: puntos de retiro


def test_vtex_no_invierte_las_coordenadas():
    """VTEX manda `[lon, lat]` y el mapa espera `[lat, lon]`.

    Es el error que este módulo tiene que no cometer nunca: el par invertido
    sigue siendo dos números válidos, así que nada explota — simplemente Buenos
    Aires aparece en China. Los valores de acá son los de la respuesta real.
    """
    item = load_json("carrefour_pickup_points.json")["items"][0]
    location = vtex_pickup.parse_pickup_point(item, "carrefour-ar")

    assert location is not None
    # En la fixture el par crudo es [-58.442158, -34.59662].
    assert location.latitude == pytest.approx(-34.59662)
    assert location.longitude == pytest.approx(-58.442158)
    # Argentina continental: latitud bien al sur, longitud bien al oeste.
    assert -56 < location.latitude < -21
    assert -74 < location.longitude < -53


def test_vtex_mapea_los_campos_utiles():
    item = load_json("carrefour_pickup_points.json")["items"][0]
    location = vtex_pickup.parse_pickup_point(item, "carrefour-ar")

    assert location.external_id == "carrefourar0899_0780PR"
    assert location.name == "Av Corrientes 5573 Express"
    assert location.address == "Avenida Corrientes 5573"
    assert location.postal_code == "1414"
    assert location.source == SOURCE_API


@pytest.mark.parametrize("index,motivo", [(4, "sin coordenadas"), (5, "en (0,0)")])
def test_vtex_descarta_lo_que_no_se_puede_ubicar(index, motivo):
    """Una sucursal sin coordenada se saltea, no rompe el barrido.

    El (0,0) importa aparte: es el Atlántico frente a África, y VTEX lo usa como
    «sin cargar». Aceptarlo pondría un marker en el océano.
    """
    item = load_json("carrefour_pickup_points.json")["items"][index]
    assert vtex_pickup.parse_pickup_point(item, "carrefour-ar") is None, motivo


@pytest.mark.asyncio
@respx.mock
async def test_vtex_pagina_y_deduplica():
    """Las siembras se solapan; la misma sucursal no se cuenta dos veces."""
    payload = load_json("carrefour_pickup_points.json")
    respx.get(url__regex=r".*/api/checkout/pub/pickup-points.*").mock(
        return_value=httpx.Response(200, json=payload)
    )

    found = await vtex_pickup.fetch_locations(
        "carrefour-ar",
        "https://www.carrefour.com.ar",
        user_agent="test",
        min_interval_ms=0,
        seeds=(("CABA", -34.6, -58.4), ("La Plata", -34.9, -57.9)),
    )

    # 4 ubicables en la fixture; dos siembras devuelven lo mismo y no duplican.
    assert len(found) == 4
    assert len({loc.external_id for loc in found}) == 4


# --------------------------------------------------------------- Coto: scraping


def test_coto_parsea_la_tabla():
    stores = coto_scrape.parse_stores(load_text("coto_sucursales.html"))

    assert len(stores) == 6
    assert len({s.external_id for s in stores}) == 6

    first = next(s for s in stores if s.external_id == "91")
    assert first.address == "Agüero 616"
    assert first.city == "Capital Federal"
    assert first.chain_slug == "coto-ar"
    # Sin coordenadas todavía: las pone el geocodificador.
    assert first.source == "geocoded"


def test_coto_limpia_el_icono_pegado_a_la_direccion():
    """La celda de dirección trae un `<img>` de autocobro después del texto.

    Sin recortarlo, la dirección se va a geocodificar con basura pegada.
    """
    stores = coto_scrape.parse_stores(load_text("coto_sucursales.html"))
    for store in stores:
        assert "<" not in (store.address or "")
        assert "img" not in (store.address or "").lower()


# ------------------------------------------------------------- normalización


@pytest.mark.parametrize(
    "raw,esperado",
    [
        ("Market Maipú ", "market maipu"),
        (" Market Mosconi ", "market mosconi"),
        ("Market  Jujuy I", "market jujuy i"),
        ("Hogar & Electro", "hogar electro"),
    ],
)
def test_normalize_name(raw, esperado):
    """Los nombres de Carrefour vienen con espacios de más y sin criterio.

    Son los valores reales que devuelve la API: sin normalizar, una sucursal no
    matchea ni consigo misma entre dos endpoints de la misma cadena.
    """
    assert normalize_name(raw) == esperado


def test_haversine():
    # Obelisco a Plaza de Mayo: ~1,4 km en línea recta.
    assert haversine_km(-34.6037, -58.3816, -34.6083, -58.3712) == pytest.approx(
        1.06, abs=0.3
    )
    assert haversine_km(-34.6, -58.4, -34.6, -58.4) == 0


# ------------------------------------------------------------ geocodificación


def test_address_query_agrega_contexto():
    """Sin localidad y país, Nominatim no desambigua.

    Hay una calle Maipú en CABA y una ciudad Maipú en Mendoza.
    """
    location = Location(
        chain_slug="coto-ar",
        external_id="91",
        name="Coto Abasto",
        latitude=0.0,
        longitude=0.0,
        source="geocoded",
        address="Agüero 616",
        city="Capital Federal",
    )
    assert address_query(location) == "Agüero 616, Capital Federal, Argentina"


@pytest.mark.asyncio
@respx.mock
async def test_geocoder_no_repite_la_misma_direccion(session, user):
    """La segunda consulta sale de la caché, no de la red.

    A un request por segundo, volver a preguntar lo mismo es el costo que esta
    caché existe para evitar.
    """
    route = respx.get(url__regex=r"https://nominatim\.openstreetmap\.org/search.*").mock(
        return_value=httpx.Response(
            200,
            json=[{"lat": "-34.6037", "lon": "-58.3816", "display_name": "Agüero 616"}],
            headers={"content-type": "application/json"},
        )
    )

    async with Geocoder(session, min_interval_ms=0) as geocoder:
        first = await geocoder.resolve("Agüero 616, Capital Federal, Argentina")
        second = await geocoder.resolve("agüero 616,  Capital Federal, Argentina  ")

    assert first == second == (pytest.approx(-34.6037), pytest.approx(-58.3816))
    # La segunda difiere en caja y espaciado: normalizada es la misma consulta.
    assert route.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_geocoder_cachea_los_fracasos(session, user):
    """Una dirección que Nominatim no conoce no se reintenta.

    Sin esto se gastaría el segundo de espera en cada corrida del script para
    volver a no obtener nada.
    """
    route = respx.get(url__regex=r"https://nominatim\.openstreetmap\.org/search.*").mock(
        return_value=httpx.Response(
            200, json=[], headers={"content-type": "application/json"}
        )
    )

    async with Geocoder(session, min_interval_ms=0) as geocoder:
        assert await geocoder.resolve("Calle Inventada 1, Argentina") is None
        assert await geocoder.resolve("Calle Inventada 1, Argentina") is None

    assert route.call_count == 1
    cached = await GeocodeCacheRepository(session).get("Calle Inventada 1, Argentina")
    assert cached is not None and cached.latitude is None


# ---------------------------------------------------------------- la búsqueda


async def _seed(session):
    chains = ChainRepository(session)
    stores = StoreRepository(session)
    carrefour = await chains.upsert(Chain(slug="carrefour-ar", display_name="Carrefour", supports_store_prices=True))
    coto = await chains.upsert(Chain(slug="coto-ar", display_name="Coto", supports_store_prices=True))
    disco = await chains.upsert(Chain(slug="disco-ar", display_name="Disco", supports_store_prices=False))

    def loc(slug, ext, name, lat, lon, cp=None, source=SOURCE_API):
        return Location(
            chain_slug=slug, external_id=ext, name=name, latitude=lat,
            longitude=lon, source=source, postal_code=cp,
        )

    # Dos Carrefour: uno en Villa Crespo (CP 1414) y otro lejos, en Córdoba.
    await stores.upsert_location(carrefour.id, loc("carrefour-ar", "cf-cerca", "Hiper Warnes", -34.5989, -58.4416, "1414"))
    await stores.upsert_location(carrefour.id, loc("carrefour-ar", "cf-lejos", "Hiper Córdoba", -31.4201, -64.1888, "5000"))
    await stores.upsert_location(coto.id, loc("coto-ar", "91", "Coto Abasto", -34.6037, -58.4100, None, "geocoded"))
    return carrefour, coto, disco


@pytest.mark.asyncio
async def test_centro_sale_de_las_sucursales_del_cp(session, user):
    """Ubicar al usuario no necesita red si hay sucursales en su CP."""
    await _seed(session)
    center = await resolve_center(session, user_id=user.id, postal_code="1414")

    assert center is not None
    assert center.latitude == pytest.approx(-34.5989)
    assert center.longitude == pytest.approx(-58.4416)
    # De dónde salió el centro viaja con él: no es lo mismo el promedio de un CP
    # que la dirección exacta, y la pantalla tiene que poder decirlo.
    assert center.source == "postal_code"


@pytest.mark.asyncio
async def test_sin_sucursales_en_el_cp_y_sin_geocoder_no_hay_centro(session, user):
    await _seed(session)
    assert await resolve_center(session, user_id=user.id, postal_code="9999") is None


@pytest.mark.asyncio
async def test_una_sucursal_por_cadena_la_mas_cercana(session, user):
    await _seed(session)
    center = await resolve_center(session, user_id=user.id, postal_code="1414")
    assert center is not None
    nearest = await nearest_by_chain(session, user_id=user.id, center=center.point)

    by_slug = {n.chain_slug: n for n in nearest}
    # Disco no tiene ninguna ubicada: no aparece, y eso no es un error.
    assert set(by_slug) == {"carrefour-ar", "coto-ar"}
    # De los dos Carrefour gana el de Villa Crespo, no el de Córdoba.
    assert by_slug["carrefour-ar"].external_id == "cf-cerca"
    assert by_slug["carrefour-ar"].distance_km == pytest.approx(0, abs=0.1)
    # La procedencia de la coordenada viaja hasta la respuesta.
    assert by_slug["coto-ar"].location_source == "geocoded"


@pytest.mark.asyncio
async def test_sin_centro_igual_devuelve_markers(session, user):
    """Sin poder ubicar al usuario se dibujan igual, sin orden de cercanía.

    Devolver vacío escondería ubicaciones que sí tenemos.
    """
    await _seed(session)
    nearest = await nearest_by_chain(session, user_id=user.id, center=None)

    assert {n.chain_slug for n in nearest} == {"carrefour-ar", "coto-ar"}
    assert all(n.distance_km is None for n in nearest)


@pytest.mark.asyncio
async def test_upsert_location_no_pisa_el_nombre_del_catalogo(session, user):
    """El nombre bueno es el del catálogo de precios, no el del localizador."""
    chains = ChainRepository(session)
    stores = StoreRepository(session)
    carrefour = await chains.upsert(Chain(slug="carrefour-ar", display_name="Carrefour", supports_store_prices=True))

    from app.models import catalog as domain

    await stores.upsert(
        carrefour.id,
        domain.Store(chain_slug="carrefour-ar", external_id="cf1", name="Hiper Warnes"),
    )
    await stores.upsert_location(
        carrefour.id,
        Location(
            chain_slug="carrefour-ar", external_id="cf1", name="Warnes Hiper ",
            latitude=-34.6, longitude=-58.4, source=SOURCE_API,
        ),
    )

    located = await stores.located("carrefour-ar")
    assert len(located) == 1
    assert located[0].name == "Hiper Warnes"
    assert located[0].latitude == pytest.approx(-34.6)


# ------------------------------------------------- sucursales cargadas a mano


@pytest.mark.asyncio
async def test_la_sucursal_manual_le_gana_a_la_automatica_aunque_este_mas_lejos(session, user):
    """Es la regla que hace que cargar una a mano sirva para algo.

    El caso real: Coto publica direcciones en texto, geocodificarlas le erra, y
    el usuario carga a mano la que tiene enfrente. Si la automática ganara por
    estar —según una coordenada que ya sabemos dudosa— cuatro cuadras más cerca,
    ese trabajo no habría servido de nada.
    """
    _carrefour, coto, _disco = await _seed(session)
    await CustomStoreRepository(session, user.id).create(
        {
            "chain_id": coto.id,
            "name": "Coto de la esquina",
            # A 1,5 km del centro; la automática de `_seed` está a ~600 m.
            "latitude": -34.6150,
            "longitude": -58.4350,
        }
    )

    center = await resolve_center(session, user_id=user.id, postal_code="1414")
    assert center is not None
    by_slug = {n.chain_slug: n for n in await nearest_by_chain(session, user_id=user.id, center=center.point)}

    assert by_slug["coto-ar"].name == "Coto de la esquina"
    assert by_slug["coto-ar"].location_source == "manual"
    # El id vuelve para poder editarla o borrarla desde el propio marker.
    assert by_slug["coto-ar"].custom_store_id is not None
    # Y el `external_id` no finge ser del catálogo de la cadena: por eso esta
    # sucursal no puede resolver precios, y decirlo evita que alguien lo intente.
    assert by_slug["coto-ar"].external_id.startswith("manual:")
    # La cadena que no tocó nadie sigue saliendo de la fuente automática.
    assert by_slug["carrefour-ar"].location_source == SOURCE_API


@pytest.mark.asyncio
async def test_entre_dos_manuales_de_la_misma_cadena_gana_la_mas_cercana(session, user):
    _carrefour, coto, _disco = await _seed(session)
    repo = CustomStoreRepository(session, user.id)
    for name, lat, lon in [
        ("Coto lejos", -34.9000, -58.4350),
        ("Coto cerca", -34.6000, -58.4400),
    ]:
        await repo.create(
            {"chain_id": coto.id, "name": name, "latitude": lat, "longitude": lon}
        )

    center = await resolve_center(session, user_id=user.id, postal_code="1414")
    assert center is not None
    by_slug = {n.chain_slug: n for n in await nearest_by_chain(session, user_id=user.id, center=center.point)}
    assert by_slug["coto-ar"].name == "Coto cerca"


# ----------------------------------------------------------- centro del mapa


@pytest.mark.asyncio
async def test_el_domicilio_ubicado_le_gana_al_promedio_del_codigo_postal(session, user):
    """Es el motivo de la función: un CP abarca demasiado para decir «cercana»."""
    await _seed(session)
    await AddressRepository(session, user.id).create(
        {
            "label": "Casa",
            "street": "Gallo",
            "number": "250",
            "latitude": -34.6056,
            "longitude": -58.4135,
            "geo_source": "geocoded",
            "is_default": True,
        }
    )

    center = await resolve_center(session, user_id=user.id, postal_code="1414")
    assert center is not None
    assert center.source == "address"
    assert center.label == "Casa"
    assert center.latitude == pytest.approx(-34.6056)


@pytest.mark.asyncio
async def test_un_domicilio_sin_coordenadas_no_sirve_de_centro(session, user):
    """Guardar la dirección y ubicarla son dos cosas distintas.

    Un domicilio anotado sin coordenadas no puede mover el mapa, y tomarlo igual
    dejaría el centro en `None` en vez de caer al código postal, que sí funciona.
    """
    await _seed(session)
    await AddressRepository(session, user.id).create({"label": "Casa", "is_default": True})

    center = await resolve_center(session, user_id=user.id, postal_code="1414")
    assert center is not None
    assert center.source == "postal_code"


@pytest.mark.asyncio
async def test_unas_coordenadas_explicitas_le_ganan_a_todo(session, user):
    await _seed(session)
    await AddressRepository(session, user.id).create(
        {"label": "Casa", "latitude": -34.6056, "longitude": -58.4135, "is_default": True}
    )

    center = await resolve_center(session, user_id=user.id, postal_code="1414", explicit=(-31.4201, -64.1888))
    assert center is not None
    assert center.source == "explicit"
    assert center.latitude == pytest.approx(-31.4201)


# ------------------------------------------------------------ Georef: direcciones


@respx.mock
@pytest.mark.asyncio
async def test_georef_devuelve_todas_las_opciones_sin_elegir_por_el_usuario():
    """«Av. Belgrano 950» existe en tres lugares y hay que mostrarlos.

    Es la respuesta real de la API: quedarse con la primera pone el marker en
    Tres Arroyos cuando la persona vive en Comuna 1.
    """
    respx.get(url__startswith=f"{georef.BASE_URL}{georef.SEARCH_PATH}").mock(
        return_value=httpx.Response(200, json=load_json("georef_belgrano.json"))
    )
    async with georef.GeorefGeocoder() as geocoder:
        found = await geocoder.search("Av. Belgrano 950", province="Buenos Aires")

    assert [c.label for c in found] == [
        "AV BELGRANO 950, Tres Arroyos, Buenos Aires",
        "AV BELGRANO 950, Comuna 1, Ciudad Autónoma de Buenos Aires",
        "AV BELGRANO 950, Saladillo, Buenos Aires",
    ]
    assert found[1].latitude == pytest.approx(-34.612992)
    assert found[1].city == "Comuna 1"


@respx.mock
@pytest.mark.asyncio
async def test_georef_no_repite_la_misma_esquina():
    """Georef devuelve «GALLO 250» dos veces, en Comuna 3 y Comuna 5, con las
    mismas coordenadas. Son la misma opción escrita distinto: ofrecer las dos
    obliga a elegir entre cosas que no se diferencian en nada."""
    respx.get(url__startswith=f"{georef.BASE_URL}{georef.SEARCH_PATH}").mock(
        return_value=httpx.Response(200, json=load_json("georef_gallo.json"))
    )
    async with georef.GeorefGeocoder() as geocoder:
        found = await geocoder.search("Gallo 250", province="CABA")

    assert len(found) == 1


@respx.mock
@pytest.mark.asyncio
async def test_si_el_partido_no_da_resultados_se_reintenta_sin_el():
    """Georef contesta vacío ante un partido que no reconoce.

    Que alguien escriba mal el partido —o que la calle esté en el de al lado— no
    puede terminar en «esa dirección no existe» cuando sí existe.
    """
    route = respx.get(url__startswith=f"{georef.BASE_URL}{georef.SEARCH_PATH}").mock(
        side_effect=[
            httpx.Response(200, json={"cantidad": 0, "direcciones": []}),
            httpx.Response(200, json=load_json("georef_gallo.json")),
        ]
    )
    async with georef.GeorefGeocoder() as geocoder:
        found = await geocoder.search("Gallo 250", city="Balvanera", province="CABA")

    assert len(found) == 1
    assert route.call_count == 2
    assert "departamento" in route.calls[0].request.url.params
    assert "departamento" not in route.calls[1].request.url.params


@respx.mock
@pytest.mark.asyncio
async def test_una_calle_sin_altura_no_es_un_punto():
    """Georef conoce la calle pero no dónde cae el número: viene sin `ubicacion`.

    Sirve para autocompletar, no para poner un marker, así que se descarta en
    vez de guardarse con las coordenadas en cero.
    """
    respx.get(url__startswith=f"{georef.BASE_URL}{georef.SEARCH_PATH}").mock(
        return_value=httpx.Response(
            200,
            json={
                "cantidad": 1,
                "direcciones": [
                    {"nomenclatura": "GALLO, Comuna 3", "ubicacion": None}
                ],
            },
        )
    )
    async with georef.GeorefGeocoder() as geocoder:
        assert await geocoder.search("Gallo") == []


# ------------------------------------------- carga masiva de Coto: qué se guarda

from dataclasses import replace  # noqa: E402

from scripts.refresh_store_locations import geocoding_hint, plausible  # noqa: E402


def _coto(address: str, city: str) -> Location:
    return Location(
        chain_slug="coto-ar", external_id="1", name="Coto", latitude=0.0,
        longitude=0.0, source="geocoded", address=address, city=city,
    )


def test_capital_federal_se_traduce_a_provincia():
    """«Capital Federal» no es un partido y como filtro no sirve; como provincia sí."""
    hint, zone = geocoding_hint(_coto("Av. San Martín 2095", "Capital Federal"))

    assert hint.province == "Ciudad Autónoma de Buenos Aires"
    assert hint.city is None
    assert zone is not None and not zone.skip


def test_la_ciudad_metida_en_la_direccion_se_saca_y_se_usa():
    """Coto escribe «Urquiza 1644 (Rosario)».

    Sin separar la ciudad, la dirección se resuelve a doscientos kilómetros de
    Rosario y dentro de la misma provincia, así que el filtro por provincia no
    lo agarra.
    """
    hint, _zone = geocoding_hint(_coto("Urquiza 1644 (Rosario)", "Santa Fe"))

    assert hint.address == "Urquiza 1644"
    assert hint.city == "Rosario"
    assert hint.province == "Santa Fe"


def test_el_conurbano_no_se_geocodifica():
    """Medido: acotando por provincia y validando contra el AMBA entran 22 y
    ocho caen en el partido equivocado. Coto publica «Zona Norte» y una calle
    que se repite en veinte partidos; no hay filtro que invente lo que falta."""
    for zona in ("Zona Norte", "Zona Sur", "Zona Oeste"):
        _hint, zone = geocoding_hint(_coto("Av. Mitre 2951", zona))
        assert zone is not None and zone.skip, zona


def test_una_coordenada_fuera_de_su_zona_se_descarta():
    """Es el filtro que separa 62 ubicaciones buenas de 98 con cuarenta mentiras.

    Sin él, «Coto Fisherton (Rosario)» queda guardado en Mendoza — y en el mapa
    una coordenada equivocada se ve exactamente igual que una correcta.
    """
    _hint, zone = geocoding_hint(_coto("Av. Cabildo 4125", "Capital Federal"))

    en_caba = _coto("Av. Cabildo 4125", "Capital Federal")
    assert plausible(replace(en_caba, latitude=-34.5620, longitude=-58.4560), zone)
    # Bahía Blanca: mismo país, otra punta.
    assert not plausible(replace(en_caba, latitude=-38.7449, longitude=-62.2266), zone)


def test_sin_caja_no_se_valida_pero_tampoco_se_rompe():
    """Las provincias sin caja definida pasan: acotar por provincia ya alcanza."""
    _hint, zone = geocoding_hint(_coto("Rivadavia 3396", "Entre Rios"))
    assert plausible(_coto("Rivadavia 3396", "Entre Rios"), zone)
