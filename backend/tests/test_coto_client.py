"""Protocolo HTTP del cliente de Coto: URLs, paginación y manejo de errores.

Lo que se defiende acá es que un fallo de Coto no se lleve puesta la búsqueda
federada, y que cuando falla se note: un error tragado que devuelve lista vacía
es indistinguible de "no hay resultados", y esconde la caída detrás de una
respuesta plausible.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx

from app.services.http import (
    ProviderBadRequest,
    ProviderBlocked,
    ProviderError,
    ProviderUnavailable,
)
from app.services.providers.coto.client import CotoClient
from app.services.providers.coto.config import COTO_AR

pytestmark = pytest.mark.asyncio

BASE = COTO_AR.base_url


def _client(**kwargs: Any) -> CotoClient:
    return CotoClient(COTO_AR, user_agent="test-agent", max_retries=1, **kwargs)


def _payload(results: list[dict[str, Any]], total: int | None = None) -> dict[str, Any]:
    return {
        "response": {
            "results": results,
            "total_num_results": total if total is not None else len(results),
        }
    }


def _result(pid: str, price: float = 100.0) -> dict[str, Any]:
    return {
        "matched_terms": ["leche"],
        "data": {
            "id": pid,
            "sku_id": f"sku{pid}",
            "sku_display_name": f"Producto {pid}",
            "price": [{"store": "200", "listPrice": price, "formatPrice": price}],
        },
    }


class TestArmadoDeLaUrl:
    @respx.mock
    async def test_manda_la_api_key_y_la_paginacion(self):
        route = respx.get(url__startswith=f"{BASE}/search/").mock(
            return_value=httpx.Response(200, json=_payload([_result("p1")]))
        )
        async with _client() as client:
            await client.search_page("leche", page=2, page_size=20)

        request = route.calls.last.request
        assert request.url.params["key"] == COTO_AR.api_key
        assert request.url.params["page"] == "2"
        assert request.url.params["num_results_per_page"] == "20"

    @respx.mock
    async def test_el_termino_va_en_el_path_y_escapado(self):
        """El término es parte de la ruta, no un query param."""
        route = respx.get(url__startswith=f"{BASE}/search/").mock(
            return_value=httpx.Response(200, json=_payload([]))
        )
        async with _client() as client:
            await client.search_page("dulce de leche")

        assert "/search/dulce%20de%20leche" in str(route.calls.last.request.url)

    @respx.mock
    async def test_no_pide_mas_del_maximo_por_pagina(self):
        route = respx.get(url__startswith=f"{BASE}/search/").mock(
            return_value=httpx.Response(200, json=_payload([]))
        )
        async with _client() as client:
            await client.search_page("leche", page_size=5000)

        params = route.calls.last.request.url.params
        assert int(params["num_results_per_page"]) == COTO_AR.max_page_size


class TestErrores:
    """Coto caído no puede romper el flujo global de búsqueda."""

    @respx.mock
    async def test_una_clave_invalida_es_un_pedido_mal_hecho(self):
        """Medido: clave inválida devuelve 400, no 401."""
        respx.get(url__startswith=f"{BASE}/search/").mock(
            return_value=httpx.Response(
                400, json={"message": "You have supplied an invalid `key`"}
            )
        )
        async with _client() as client:
            with pytest.raises(ProviderBadRequest):
                await client.search_page("leche")

    @respx.mock
    async def test_no_reintenta_los_400(self):
        """Reintentar un pedido mal armado sólo esconde el bug."""
        route = respx.get(url__startswith=f"{BASE}/search/").mock(
            return_value=httpx.Response(400, json={"message": "invalid key"})
        )
        async with _client() as client:
            with pytest.raises(ProviderBadRequest):
                await client.search_page("leche")
        assert route.call_count == 1

    @respx.mock
    async def test_el_timeout_sale_como_no_disponible(self):
        respx.get(url__startswith=f"{BASE}/search/").mock(
            side_effect=httpx.ReadTimeout("demasiado lento")
        )
        async with _client() as client:
            with pytest.raises(ProviderUnavailable):
                await client.search_page("leche")

    @respx.mock
    async def test_reintenta_los_5xx(self):
        route = respx.get(url__startswith=f"{BASE}/search/").mock(
            side_effect=[
                httpx.Response(503, json={"message": "nope"}),
                httpx.Response(200, json=_payload([_result("p1")])),
            ]
        )
        async with CotoClient(COTO_AR, user_agent="t", max_retries=2) as client:
            page = await client.search_page("leche")
        assert route.call_count == 2
        assert len(page.items) == 1

    @respx.mock
    async def test_un_challenge_de_bot_no_se_confunde_con_datos(self):
        respx.get(url__startswith=f"{BASE}/search/").mock(
            return_value=httpx.Response(200, html="<html>Access denied</html>")
        )
        async with _client() as client:
            with pytest.raises(ProviderBlocked):
                await client.search_page("leche")

    @respx.mock
    async def test_una_respuesta_sin_bloque_response_es_error(self):
        respx.get(url__startswith=f"{BASE}/search/").mock(
            return_value=httpx.Response(200, json={"otra_cosa": []})
        )
        async with _client() as client:
            with pytest.raises(ProviderError, match="response"):
                await client.search_page("leche")

    @respx.mock
    async def test_safe_search_devuelve_vacio_en_lugar_de_romper(self):
        """Para los recorridos donde una cadena caída no debe cortar el trabajo."""
        respx.get(url__startswith=f"{BASE}/search/").mock(
            side_effect=httpx.ConnectError("sin red")
        )
        async with _client() as client:
            results, total = await client.safe_search("leche")
        assert results == []
        assert total is None


class TestPaginacionDeCategoria:
    @respx.mock
    async def test_corta_al_llegar_al_total_declarado(self):
        respx.get(url__startswith=f"{BASE}/browse/group_id/").mock(
            side_effect=[
                httpx.Response(200, json=_payload([_result("a"), _result("b")], total=3)),
                httpx.Response(200, json=_payload([_result("c")], total=3)),
                httpx.Response(200, json=_payload([], total=3)),
            ]
        )
        pages = []
        async with _client() as client:
            async for page in client.iter_group_pages("catv00001255", page_size=2):
                pages.append(page)
        assert sum(len(p.items) for p in pages) == 3

    @respx.mock
    async def test_corta_con_pagina_vacia(self):
        respx.get(url__startswith=f"{BASE}/browse/group_id/").mock(
            side_effect=[
                httpx.Response(200, json=_payload([_result("a")], total=99)),
                httpx.Response(200, json=_payload([], total=99)),
            ]
        )
        pages = []
        async with _client() as client:
            async for page in client.iter_group_pages("catv00001255", page_size=1):
                pages.append(page)
        assert len(pages) == 1
        assert not pages[-1].truncated

    @respx.mock
    async def test_avisa_cuando_corta_por_el_tope_de_paginas(self):
        """Perder catálogo en silencio es el peor modo de falla de un comparador."""
        respx.get(url__startswith=f"{BASE}/browse/group_id/").mock(
            return_value=httpx.Response(200, json=_payload([_result("a")], total=10_000))
        )
        pages = []
        async with _client() as client:
            async for page in client.iter_group_pages("catv00001255", page_size=1):
                pages.append(page)
        assert pages[-1].truncated
        assert len(pages) == COTO_AR.max_pages + 1
