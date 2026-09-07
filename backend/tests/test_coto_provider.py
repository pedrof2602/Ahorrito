"""El proveedor de Coto contra el contrato común de `PriceProvider`.

Buena parte de lo que se prueba acá es lo que Coto **no** puede hacer. Sin
checkout público no hay precio autoritativo, y el contrato define respuestas
para eso: devolverlas es honesto, devolver precios de catálogo disfrazados de
confirmados no lo sería.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from app.models.catalog import CategoryNode, PriceScope, Store
from app.services.providers.base import PriceProvider
from app.services.providers.coto.client import CotoClient
from app.services.providers.coto.config import COTO_AR
from app.services.providers.coto.provider import CotoProvider

pytestmark = pytest.mark.asyncio

BASE = COTO_AR.base_url
FIXTURES = Path(__file__).parent / "fixtures" / "coto"


def fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def _provider() -> CotoProvider:
    return CotoProvider(COTO_AR, CotoClient(COTO_AR, user_agent="t", max_retries=1))


class TestContrato:
    async def test_cumple_el_protocolo_de_proveedor(self):
        """`PriceProvider` es un Protocol: se cumple por estructura."""
        assert isinstance(_provider(), PriceProvider)

    async def test_declara_la_cadena(self):
        chain = _provider().chain
        assert chain.slug == "coto-ar"
        assert chain.display_name == "Coto"
        assert chain.supports_store_prices is True
        # Constructor.io no tiene canales de venta: la palanca es la sucursal.
        assert chain.sales_channels == ()
        assert chain.default_sales_channel is None


class TestBusqueda:
    @respx.mock
    async def test_devuelve_ofertas_mapeadas(self):
        respx.get(url__startswith=f"{BASE}/search/").mock(
            return_value=httpx.Response(200, json=fixture("search_leche"))
        )
        provider = _provider()
        offers = await provider.search("leche", limit=10)
        await provider.aclose()

        assert offers
        assert all(o.product.chain_slug == "coto-ar" for o in offers)
        assert all(o.offer.price_cents > 0 for o in offers)

    @respx.mock
    async def test_respeta_el_limite(self):
        respx.get(url__startswith=f"{BASE}/search/").mock(
            return_value=httpx.Response(200, json=fixture("search_leche"))
        )
        provider = _provider()
        offers = await provider.search("leche", limit=2)
        await provider.aclose()
        assert len(offers) <= 2

    @respx.mock
    async def test_un_sales_channel_de_vtex_no_lo_hace_fallar(self):
        """Pedir un canal de Carrefour no puede dejarte sin los precios de Coto.

        El contrato dice que la cadena cae a lo que pueda resolver, no que falle.
        """
        respx.get(url__startswith=f"{BASE}/search/").mock(
            return_value=httpx.Response(200, json=fixture("search_leche"))
        )
        provider = _provider()
        offers = await provider.search("leche", sales_channel=3, limit=5)
        await provider.aclose()
        assert offers
        assert offers[0].offer.price_scope is PriceScope.NATIONAL

    @respx.mock
    async def test_filtra_el_fallback_semantico(self):
        """Buscar algo inexistente devuelve remeras; no deben salir como resultado."""
        respx.get(url__startswith=f"{BASE}/search/").mock(
            return_value=httpx.Response(200, json=fixture("search_sin_match"))
        )
        provider = _provider()
        offers = await provider.search("zzzqqxnotaproduct", limit=10)
        await provider.aclose()
        assert offers == []

    @respx.mock
    async def test_con_sucursal_devuelve_precio_de_sucursal(self):
        respx.get(url__startswith=f"{BASE}/search/").mock(
            return_value=httpx.Response(200, json=fixture("search_leche"))
        )
        store = Store(chain_slug="coto-ar", external_id="133", name="Sucursal 133")
        provider = _provider()
        offers = await provider.search("leche", store=store, limit=10)
        await provider.aclose()

        scoped = [o for o in offers if o.offer.price_scope is PriceScope.STORE]
        assert scoped, "la 133 publica precio para al menos un producto de la fixture"
        assert all(o.offer.store_key == "coto-ar:133" for o in scoped)


class TestBusquedaPorEan:
    @respx.mock
    async def test_encuentra_el_producto_exacto(self):
        respx.get(url__startswith=f"{BASE}/search/").mock(
            return_value=httpx.Response(200, json=fixture("search_ean"))
        )
        provider = _provider()
        offer = await provider.search_by_ean("7790742333605")
        await provider.aclose()
        assert offer is not None
        assert offer.product.ean == "7790742333605"

    @respx.mock
    async def test_no_devuelve_un_parecido_cuando_el_ean_no_esta(self):
        """La búsqueda cae a similitud semántica; un "parecido" es el error caro."""
        respx.get(url__startswith=f"{BASE}/search/").mock(
            return_value=httpx.Response(200, json=fixture("search_leche"))
        )
        provider = _provider()
        offer = await provider.search_by_ean("7790000000000")
        await provider.aclose()
        assert offer is None


class TestLoQueCotoNoTiene:
    async def test_no_expone_sucursales_por_codigo_postal(self):
        """Lista vacía es una respuesta legítima del contrato, no un error."""
        provider = _provider()
        assert await provider.find_stores("1425") == []
        await provider.aclose()

    async def test_no_confirma_precios_contra_checkout(self):
        provider = _provider()
        assert await provider.verify_prices([("sku00508911", 1)]) == []
        await provider.aclose()

    async def test_no_simula_la_canasta(self):
        """`None` evita que se compare un total inventado contra el de Carrefour."""
        provider = _provider()
        assert await provider.simulate_basket([("sku00508911", 2)]) is None
        await provider.aclose()


class TestCategorias:
    @respx.mock
    async def test_arma_el_arbol_de_categorias(self):
        respx.get(url__startswith=f"{BASE}/search/").mock(
            return_value=httpx.Response(200, json=fixture("category_tree"))
        )
        provider = _provider()
        tree = await provider.category_tree(3)
        await provider.aclose()
        assert tree
        assert any(node.children for node in tree)

    @respx.mock
    async def test_recorre_una_categoria(self):
        respx.get(url__startswith=f"{BASE}/browse/group_id/").mock(
            side_effect=[
                httpx.Response(200, json=fixture("search_leche")),
                httpx.Response(200, json={"response": {"results": [], "total_num_results": 4}}),
            ]
        )
        provider = _provider()
        node = CategoryNode(id=1295, name="Lácteos", id_path=(1255, 1295))
        offers = [o async for o in provider.iter_category(node)]
        await provider.aclose()
        assert offers
        assert all(o.product.chain_slug == "coto-ar" for o in offers)
