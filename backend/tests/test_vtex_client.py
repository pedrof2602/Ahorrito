"""Protocolo HTTP del cliente VTEX: paginación, topes, codificación y errores."""

from __future__ import annotations

import httpx
import pytest
import respx

from app.models.catalog import PriceScope, Store
from app.services.http import ProviderBadRequest, ProviderBlocked
from app.services.providers.vtex.client import VTEXClient, encode_params
from app.services.providers.vtex.stores import CARREFOUR_AR, DISCO_AR

SEARCH = "/api/catalog_system/pub/products/search/"


def _client(config):
    return VTEXClient(config, user_agent="test-agent", max_retries=1)


def _product(pid: str) -> dict:
    return {
        "productId": pid,
        "productName": f"Producto {pid}",
        "items": [{
            "itemId": f"sku{pid}",
            "ean": "7791720023969",
            "sellers": [{
                "sellerId": "1",
                "sellerDefault": True,
                "commertialOffer": {"Price": 100.0, "AvailableQuantity": 5},
            }],
        }],
    }


class TestCodificacionDeQuery:
    def test_los_espacios_van_como_percent_20_no_como_plus(self):
        """El WAF de VTEX rechaza `+` con 'Scripts are not allowed!'.

        httpx codifica con `+` por defecto, así que cualquier búsqueda de más de
        una palabra fallaría sin esta corrección.
        """
        encoded = encode_params({"ft": "dulce de leche"})
        assert encoded == "ft=dulce%20de%20leche"
        assert "+" not in encoded

    def test_repite_la_clave_para_valores_multiples(self):
        """VTEX espera fq repetido, no una lista serializada."""
        encoded = encode_params({"fq": ["C:/1/2/", "B:5"]})
        assert encoded == "fq=C:/1/2/&fq=B:5"

    def test_omite_los_valores_none(self):
        assert encode_params({"ft": "leche", "sc": None}) == "ft=leche"


@pytest.mark.asyncio
class TestRegionalizacion:
    async def test_carrefour_manda_el_sales_channel(self):
        async with _client(CARREFOUR_AR) as client:
            with respx.mock(base_url=CARREFOUR_AR.base_url) as mock:
                route = mock.get(SEARCH).mock(
                    return_value=httpx.Response(200, json=[_product("1")])
                )
                await client.search("leche", sales_channel=3, limit=1)
                assert "sc=3" in str(route.calls[0].request.url)

    async def test_disco_nunca_manda_sales_channel(self):
        """Disco responde 'sc is inactive' a cualquier sc: no hay que mandarlo."""
        async with _client(DISCO_AR) as client:
            with respx.mock(base_url=DISCO_AR.base_url) as mock:
                route = mock.get(SEARCH).mock(
                    return_value=httpx.Response(200, json=[_product("1")])
                )
                await client.search("leche", sales_channel=1, limit=1)
                assert "sc=" not in str(route.calls[0].request.url)

    async def test_un_sales_channel_inactivo_cae_al_canal_por_defecto(self):
        """Antes levantaba ValueError. En un fan-out entre supermercados eso
        significaba que pedir un canal de Carrefour te dejaba sin los precios de
        Disco, así que ahora degrada al canal por defecto."""
        async with _client(CARREFOUR_AR) as client:
            with respx.mock(base_url=CARREFOUR_AR.base_url) as mock:
                route = mock.get(SEARCH).mock(
                    return_value=httpx.Response(200, json=[_product("1")])
                )
                await client.search("leche", sales_channel=2, limit=1)

        url = str(route.calls[0].request.url)
        assert f"sc={CARREFOUR_AR.default_sales_channel}" in url


@pytest.mark.asyncio
class TestPaginacion:
    async def test_206_continua_y_200_termina(self):
        """206 = página parcial, 200 = esto era todo. Es el terminador limpio."""
        async with _client(DISCO_AR) as client:
            with respx.mock(base_url=DISCO_AR.base_url) as mock:
                mock.get(SEARCH).mock(side_effect=[
                    httpx.Response(206, json=[_product(str(i)) for i in range(50)],
                                   headers={"resources": "0-49/60"}),
                    httpx.Response(200, json=[_product(str(i)) for i in range(50, 60)],
                                   headers={"resources": "50-59/60"}),
                ])
                pages = [p async for p in client.iter_search_pages(term="leche")]
        assert [len(p.items) for p in pages] == [50, 10]
        assert pages[0].total == 60

    async def test_nunca_pide_mas_de_50_por_pagina(self):
        """`_to - _from + 1 > 50` devuelve 400."""
        async with _client(DISCO_AR) as client:
            with respx.mock(base_url=DISCO_AR.base_url) as mock:
                route = mock.get(SEARCH).mock(
                    return_value=httpx.Response(200, json=[_product("1")])
                )
                await client.search("leche", limit=500)
                params = route.calls[0].request.url.params
                assert int(params["_to"]) - int(params["_from"]) + 1 <= 50

    async def test_respeta_el_limite_pedido(self):
        async with _client(DISCO_AR) as client:
            with respx.mock(base_url=DISCO_AR.base_url) as mock:
                mock.get(SEARCH).mock(
                    return_value=httpx.Response(206, json=[_product(str(i)) for i in range(3)])
                )
                assert len(await client.search("leche", limit=3)) == 3

    async def test_el_tope_de_offset_corta_y_marca_truncado(self):
        """Pasado el offset 2500 VTEX devuelve 400. No es reintentable: es el
        final de lo alcanzable. Marcarlo evita creer que se cubrió todo."""
        async with _client(DISCO_AR) as client:
            with respx.mock(base_url=DISCO_AR.base_url) as mock:
                mock.get(SEARCH).mock(side_effect=[
                    httpx.Response(206, json=[_product(str(i)) for i in range(50)],
                                   headers={"resources": "0-49/9999"}),
                    httpx.Response(400, json="Parameter _from can't be greater than 2500"),
                ])
                pages = [p async for p in client.iter_search_pages(term="leche")]
        assert pages[-1].truncated is True
        assert pages[-1].items == []

    async def test_no_reintenta_los_400(self):
        """Reintentar un error de parámetros solo esconde el bug."""
        async with _client(DISCO_AR) as client:
            with respx.mock(base_url=DISCO_AR.base_url) as mock:
                route = mock.get(SEARCH).mock(
                    return_value=httpx.Response(400, json="Bad Request")
                )
                with pytest.raises(ProviderBadRequest):
                    [p async for p in client.iter_search_pages(term="leche")]
                assert route.call_count == 1

    async def test_un_400_en_la_primera_pagina_no_se_confunde_con_fin_de_datos(self):
        """Un `fq` inválido o un sc inactivo fallan en la página 0.

        Devolver "sin resultados" ahí sería mentir: el comparador mostraría que
        el producto no existe cuando en realidad la consulta estaba mal armada.
        """
        async with _client(DISCO_AR) as client:
            with respx.mock(base_url=DISCO_AR.base_url) as mock:
                mock.get(SEARCH).mock(
                    return_value=httpx.Response(400, json="Bad Request! Scripts are not allowed!")
                )
                with pytest.raises(ProviderBadRequest):
                    await client.search("dulce de leche", limit=5)


@pytest.mark.asyncio
class TestRespuestasRaras:
    async def test_el_string_suelto_de_sales_channel_da_error_claro(self):
        """VTEX devuelve HTTP 200 con el body `"sc is inactive"`.

        Sin la guarda, el mapper revienta con
        `TypeError: string indices must be integers` lejos de la causa.
        """
        async with _client(CARREFOUR_AR) as client:
            with respx.mock(base_url=CARREFOUR_AR.base_url) as mock:
                mock.get(SEARCH).mock(
                    return_value=httpx.Response(200, json="sc is inactive")
                )
                with pytest.raises(ProviderBadRequest, match="sc is inactive"):
                    await client.search("leche", sales_channel=1, limit=1)

    async def test_el_html_de_un_challenge_no_se_confunde_con_datos(self):
        async with _client(DISCO_AR) as client:
            with respx.mock(base_url=DISCO_AR.base_url) as mock:
                mock.get(SEARCH).mock(return_value=httpx.Response(
                    200, text="<html>Just a moment...</html>",
                    headers={"content-type": "text/html"},
                ))
                with pytest.raises(ProviderBlocked):
                    await client.search("leche", limit=1)

    async def test_el_dict_de_error_de_regions_no_pasa_como_dato(self):
        """Disco devuelve 500 con {'error': {'code': 'ORD021.6'}}."""
        async with _client(CARREFOUR_AR) as client:
            with respx.mock(base_url=CARREFOUR_AR.base_url) as mock:
                mock.get("/api/checkout/pub/regions").mock(
                    return_value=httpx.Response(200, json={"error": {"code": "ORD021.6"}})
                )
                with pytest.raises(ProviderBadRequest, match="ORD021.6"):
                    await client.regions("1425")

    async def test_disco_no_consulta_regions_porque_no_las_soporta(self):
        """Sabemos que devuelve 500: no vale la pena gastar el request."""
        async with _client(DISCO_AR) as client:
            with respx.mock(base_url=DISCO_AR.base_url, assert_all_called=False) as mock:
                route = mock.get("/api/checkout/pub/regions")
                assert await client.regions("1425") == []
                assert route.call_count == 0


class TestCadenaDeResolucionDePrecio:
    """`resolve_price_scope` elige el nivel más específico posible y declara
    cuál alcanzó. Es pura: no toca la red."""

    def test_sin_nada_pedido_cae_a_nacional(self):
        assert CARREFOUR_AR.resolve_price_scope() == (1, PriceScope.NATIONAL)

    def test_un_canal_activo_da_scope_de_canal(self):
        assert CARREFOUR_AR.resolve_price_scope(3) == (3, PriceScope.CHANNEL)

    def test_un_canal_inactivo_cae_a_nacional_sin_fallar(self):
        """Pedir un canal que la cadena no tiene no puede tirar la comparación."""
        assert CARREFOUR_AR.resolve_price_scope(2) == (1, PriceScope.NATIONAL)

    def test_una_cadena_sin_regionalizacion_ignora_el_canal(self):
        """Disco responde 'sc is inactive' a cualquier sc."""
        assert DISCO_AR.resolve_price_scope(3) == (None, PriceScope.NATIONAL)

    def test_la_sucursal_gana_sobre_el_canal_pedido(self):
        """Si la cadena supiera el canal de la sucursal, ese es el más específico."""
        store = Store(
            chain_slug="carrefour-ar",
            external_id="carrefourar0026",
            name="Hiper Warnes",
            sales_channel=5,
        )
        assert CARREFOUR_AR.resolve_price_scope(3, store) == (5, PriceScope.STORE)

    def test_una_sucursal_sin_canal_atribuido_no_da_scope_de_sucursal(self):
        """Es el caso real hoy: `/regions` identifica la sucursal pero no su
        canal, así que el precio no es atribuible a ella."""
        store = Store(
            chain_slug="carrefour-ar", external_id="carrefourar0026", name="Hiper Warnes"
        )
        assert CARREFOUR_AR.resolve_price_scope(None, store) == (1, PriceScope.NATIONAL)

    def test_una_sucursal_sin_canal_no_pisa_el_canal_pedido(self):
        store = Store(
            chain_slug="carrefour-ar", external_id="carrefourar0026", name="Hiper Warnes"
        )
        assert CARREFOUR_AR.resolve_price_scope(3, store) == (3, PriceScope.CHANNEL)
