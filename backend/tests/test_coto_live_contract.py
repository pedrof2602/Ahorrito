"""Contrato contra la API real de Coto (Constructor.io).

Deseleccionados por defecto (`addopts = -m 'not live'`). Correr con:

    ./venv/bin/python -m pytest -m live -q tests/test_coto_live_contract.py

No son tests de nuestro código: son el detector de deriva. Todo el mapeo de Coto
se apoya en tres hechos medidos que la API no documenta —`formatPrice` es precio
por unidad, la sucursal 200 es la de referencia, y `matched_terms` distingue el
match léxico del fallback semántico—. Si alguno deja de ser cierto, hay que
enterarse acá y no porque el comparador empiece a mostrar precios equivocados.
"""

from __future__ import annotations

import pytest

from app.services.http import ProviderBadRequest
from app.services.providers.coto.client import CotoClient
from app.services.providers.coto.config import COTO_AR
from app.services.providers.coto.provider import CotoProvider

pytestmark = [pytest.mark.live, pytest.mark.asyncio]


def _client() -> CotoClient:
    return CotoClient(COTO_AR, user_agent="compras-app-contract-test")


class TestContratoBasico:
    async def test_la_busqueda_devuelve_productos_con_precio(self):
        async with _client() as client:
            results, total = await client.search("leche", limit=5)
        assert results, "Coto no devolvió productos para 'leche'"
        assert total and total > 100
        assert results[0]["data"]["price"], "el producto vino sin array de precios"

    async def test_la_busqueda_multipalabra_funciona(self):
        async with _client() as client:
            results, _ = await client.search("dulce de leche", limit=3)
        assert results

    async def test_la_clave_publica_sigue_siendo_valida(self):
        """Si Coto la rota, todo el proveedor deja de responder."""
        async with _client() as client:
            results, _ = await client.search("agua", limit=1)
        assert results

    async def test_una_clave_invalida_da_400(self):
        """Fija el modo de falla del que depende la taxonomía de errores."""
        from dataclasses import replace

        bad = replace(COTO_AR, api_key="key_invalida")
        async with CotoClient(bad, user_agent="test", max_retries=1) as client:
            with pytest.raises(ProviderBadRequest):
                await client.search("leche", limit=1)


class TestSupuestosDelMapeo:
    async def test_format_price_sigue_siendo_precio_por_unidad(self):
        """El supuesto del que depende todo el mapeo de precios.

        `formatPrice` es `listPrice` dividido por el tamaño del envase, así que
        en los envases de 1,5 L el cociente tiene que dar 1/1,5 = 0,667.

        Se mide **sólo sobre los productos que la sucursal realmente vende**, y
        ahí la relación es exacta. El catálogo de Coto arrastra fichas viejas de
        productos discontinuados —"Aceite Girasol VICENTIN 1.5 Ltr" sigue
        publicado a $335,38 con un `formatPrice` de $75,80— y en esas la fórmula
        no cierra. La señal que las separa es `store_availability`: medido sobre
        los envases de 1,5 L, los 8 disponibles dan 0,667 clavado y los 7 con
        cociente raro tienen `store_availability` vacío, sin una sola excepción
        en ninguna de las dos direcciones.

        Si `formatPrice` pasara a ser el precio de venta, el cociente se iría a 1
        y esto lo detecta.
        """
        async with _client() as client:
            results, _ = await client.search("aceite girasol 1.5", limit=30)

        comprobados = 0
        for result in results:
            data = result["data"]
            nombre = data.get("sku_display_name") or ""
            disponible = COTO_AR.default_store in set(data.get("store_availability") or [])
            if "1.5" not in nombre or not disponible:
                continue
            entry = next(
                (p for p in data.get("price") or [] if p.get("store") == COTO_AR.default_store),
                None,
            )
            if not entry or not entry.get("listPrice") or not entry.get("formatPrice"):
                continue
            ratio = entry["formatPrice"] / entry["listPrice"]
            assert ratio == pytest.approx(1 / 1.5, rel=0.02), (
                f"{nombre}: formatPrice/listPrice = {ratio:.3f}, esperado 0,667 "
                "si sigue siendo el precio por litro"
            )
            comprobados += 1

        assert comprobados >= 5, (
            f"sólo {comprobados} envases de 1,5 L disponibles; muestra insuficiente"
        )

    async def test_la_sucursal_de_referencia_sigue_siendo_la_200(self):
        """`default_store` sale de que el sufijo de `data.url` es 200."""
        async with _client() as client:
            results, _ = await client.search("leche", limit=30)

        sufijos = {(r["data"].get("url") or "").rsplit("-", 1)[-1] for r in results}
        assert sufijos == {COTO_AR.default_store}, (
            f"la sucursal de referencia cambió: {sufijos}"
        )

    async def test_la_sucursal_de_referencia_publica_el_precio_del_producto(self):
        """`listPrice` de la 200 debería coincidir con `product_list_price`."""
        async with _client() as client:
            results, _ = await client.search("leche", limit=30)

        coincidencias = 0
        total = 0
        for result in results:
            data = result["data"]
            entry = next(
                (p for p in data.get("price") or [] if p.get("store") == COTO_AR.default_store),
                None,
            )
            if not entry or data.get("product_list_price") is None:
                continue
            total += 1
            coincidencias += entry.get("listPrice") == data["product_list_price"]

        # El umbral es holgado por el mismo motivo que en el test anterior: las
        # fichas viejas del catálogo desincronizan los dos campos. Un cambio real
        # de sucursal de referencia llevaría esto cerca de cero, no a 0,85.
        assert total and coincidencias / total >= 0.8, (
            f"sólo {coincidencias}/{total} coinciden; revisar `default_store`"
        )

    async def test_el_ean_sigue_viniendo_como_numero(self):
        async with _client() as client:
            results, _ = await client.search("leche", limit=5)
        eans = [r["data"].get("product_main_ean") for r in results]
        assert any(isinstance(e, int) for e in eans), (
            "si dejó de ser int, revisar `normalize_ean`; acepta ambos, "
            "pero el cambio de tipo suele venir con otros"
        )


class TestFallbackSemantico:
    async def test_un_termino_inexistente_devuelve_resultados_igual(self):
        """El comportamiento que justifica `drop_unmatched_results`.

        Constructor.io responde 200 con productos cualesquiera cuando no hay
        match léxico. Si algún día devolviera vacío, el filtro sobra.
        """
        async with _client() as client:
            results, _ = await client.search("zzzqqxnotaproduct", limit=5)
        assert results, "ya no hace falta filtrar el fallback semántico"
        assert all(not r.get("matched_terms") for r in results), (
            "el fallback dejó de marcarse con matched_terms vacío: el filtro "
            "quedó ciego y hay que buscar otra señal"
        )

    async def test_una_busqueda_real_trae_matched_terms(self):
        """La otra mitad: el filtro no debe comerse resultados legítimos."""
        async with _client() as client:
            results, _ = await client.search("leche", limit=20)
        assert all(r.get("matched_terms") for r in results)


class TestExtremoAExtremo:
    async def test_la_busqueda_por_ean_devuelve_ese_producto(self):
        provider = CotoProvider(COTO_AR, _client())
        try:
            offer = await provider.search_by_ean("7790742333605")
        finally:
            await provider.aclose()
        assert offer is not None
        assert offer.product.ean == "7790742333605"
        assert offer.offer.price_cents > 0

    async def test_las_ofertas_llegan_listas_para_comparar(self):
        """Precio positivo, EAN normalizado y ámbito declarado: lo que la
        canasta necesita para poner a Coto al lado de Carrefour."""
        provider = CotoProvider(COTO_AR, _client())
        try:
            offers = await provider.search("leche", limit=20)
        finally:
            await provider.aclose()

        assert offers
        assert all(o.offer.price_cents > 0 for o in offers)
        assert all(o.product.name for o in offers)
        con_ean = [o for o in offers if o.product.ean]
        assert len(con_ean) > len(offers) / 2, (
            "sin EAN no hay comparación entre cadenas"
        )
