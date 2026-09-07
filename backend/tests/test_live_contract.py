"""Contrato contra las APIs reales de los supermercados.

Deseleccionados por defecto (`addopts = -m 'not live'`). Correr con:

    ./venv/bin/python -m pytest -m live -q

No son tests de nuestro código: son el detector de deriva. Si alguno falla,
alguna de las dos cadenas cambió algo y hay que revisar la config antes de que
el comparador empiece a mostrar precios equivocados en silencio.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.services.http import ProviderBadRequest, request_json
from app.services.promo_catalog import STALE_AFTER_DAYS, load_catalog
from app.services.providers.vtex.client import VTEXClient
from app.services.providers.vtex.stores import CARREFOUR_AR, DIA_AR, DISCO_AR

pytestmark = [pytest.mark.live, pytest.mark.asyncio]


def _client(config):
    return VTEXClient(config, user_agent="compras-app-contract-test")


@pytest.mark.parametrize("config", [CARREFOUR_AR, DISCO_AR, DIA_AR], ids=lambda c: c.slug)
class TestContratoBasico:
    async def test_la_busqueda_devuelve_productos_con_precio(self, config):
        async with _client(config) as client:
            products = await client.search("leche", limit=5)
        assert products, f"{config.slug} no devolvió productos para 'leche'"
        offer = products[0]["items"][0]["sellers"][0]["commertialOffer"]
        assert offer["Price"] > 0

    async def test_la_busqueda_multipalabra_sigue_funcionando(self, config):
        """Regresión del WAF: con `+` en vez de `%20` esto devuelve 400."""
        async with _client(config) as client:
            assert await client.search("dulce de leche", limit=3)

    async def test_el_arbol_de_categorias_responde(self, config):
        async with _client(config) as client:
            tree = await client.category_tree(depth=2)
        assert len(tree) > 5
        assert all("id" in node and "name" in node for node in tree)


class TestRegionalizacionCarrefour:
    async def test_las_sucursales_cambian_con_el_codigo_postal(self):
        """Si esto falla, 'precios cerca de casa' dejó de tener sentido."""
        async with _client(CARREFOUR_AR) as client:
            caba = await client.regions("1425")
            cordoba = await client.regions("5000")

        def sellers(regions):
            return {s["id"] for r in regions for s in r.get("sellers", [])}

        assert sellers(caba) != sellers(cordoba)

    async def test_los_sales_channel_declarados_siguen_activos(self):
        async with _client(CARREFOUR_AR) as client:
            for sc in CARREFOUR_AR.sales_channels:
                assert await client.search("leche", sales_channel=sc, limit=1), (
                    f"El sales channel {sc} dejó de devolver resultados"
                )

    async def test_el_canal_por_defecto_sigue_siendo_el_que_surte(self):
        """Los canales alternativos publican precios pero surten mucho menos: el
        3 es errático (20/20 en 'leche', 0/20 en 'arroz') y el 5 no tuvo stock en
        ningún término. Por eso el 1 es el `default_sales_channel`.

        Si esto se da vuelta, hay que revisar cuál conviene como piso."""
        terms = ("leche", "arroz", "yerba")
        async with _client(CARREFOUR_AR) as client:
            con_stock = dict.fromkeys(CARREFOUR_AR.sales_channels, 0)
            for sc in CARREFOUR_AR.sales_channels:
                for term in terms:
                    found = await client.search(term, sales_channel=sc, limit=20)
                    con_stock[sc] += sum(
                        1
                        for p in found
                        if p["items"][0]["sellers"][0]["commertialOffer"].get(
                            "AvailableQuantity", 0
                        )
                        > 0
                    )

        default = CARREFOUR_AR.default_sales_channel
        assert con_stock[default] > 0, "El canal por defecto se quedó sin stock"
        assert all(
            con_stock[default] >= con_stock[sc] for sc in con_stock
        ), f"El canal por defecto dejó de ser el que más surte: {con_stock}"

    async def test_el_sales_channel_sigue_moviendo_los_precios(self):
        """Es la única palanca de regionalización que nos queda: la legacy
        ignora regionId, y la Intelligent Search pierde catálogo."""
        async with _client(CARREFOUR_AR) as client:
            precios = {}
            for sc in (1, 3):
                found = await client.search("cápsulas de café", sales_channel=sc, limit=5)
                precios[sc] = {
                    p["productId"]: p["items"][0]["sellers"][0]["commertialOffer"]["Price"]
                    for p in found
                }
        comunes = set(precios[1]) & set(precios[3])
        assert comunes, "No hubo productos en común para comparar canales"
        assert any(precios[1][pid] != precios[3][pid] for pid in comunes), (
            "Ningún precio cambió entre sales channels: revisar la estrategia"
        )


class TestLimitesDelProtocolo:
    async def test_pasar_el_tope_de_offset_sigue_dando_400(self):
        async with _client(DISCO_AR) as client:
            with pytest.raises(ProviderBadRequest):
                await client._fetch_page({"ft": "leche"}, 2600, 10)

    async def test_un_sales_channel_inactivo_sigue_siendo_error(self):
        """Carrefour lo devuelve como string suelto o como 400; ambos casos
        tienen que llegar como error, no como 'sin resultados'."""
        async with _client(CARREFOUR_AR) as client:
            with pytest.raises(ProviderBadRequest):
                await client._fetch_page({"ft": "leche", "sc": 2}, 0, 5)


class TestRaresasConocidas:
    async def test_carrefour_sigue_serializando_teasers_con_mangling(self):
        """Si dejan de hacerlo, `teaser_backing_fields` sobra (no rompe, pero
        conviene saberlo)."""
        async with _client(CARREFOUR_AR) as client:
            products = await client.search("leche", limit=20)
        teasers = [
            t
            for p in products
            for i in p["items"]
            for s in i["sellers"]
            for t in (s["commertialOffer"].get("Teasers") or [])
        ]
        if not teasers:
            pytest.skip("No hubo promos en la muestra")
        assert any("k__BackingField" in k for t in teasers for k in t)

    async def test_dia_sigue_serializando_teasers_con_mangling(self):
        """DIA tiene el mismo mangling que Carrefour, y acá sí duele: si el flag
        no estuviera, `_parse_promotion` no encontraría `Name` y las promos se
        caerían en silencio. El 2x1 de las leches condensadas es el caso vivo."""
        async with _client(DIA_AR) as client:
            products = await client.search("leche", limit=30)
        teasers = [
            t
            for p in products
            for i in p["items"]
            for s in i["sellers"]
            for t in (s["commertialOffer"].get("Teasers") or [])
        ]
        if not teasers:
            pytest.skip("No hubo promos en la muestra")
        assert any("k__BackingField" in k for t in teasers for k in t)

    async def test_dia_sigue_sin_regionalizar_por_sales_channel(self):
        """La apuesta de `RegionStrategy.NONE` para DIA, en un assert.

        Medido: los canales 1 y 2 publican el mismo precio para todo SKU en común
        (49/49), y el 2 encima viene sin stock. Si algún día empiezan a diferir,
        DIA pasa a tener regionalización real y hay que revisar la estrategia.
        """
        async with _client(DIA_AR) as client:
            precios = {}
            for sc in (1, 2):
                found = await client.search("leche", sales_channel=sc, limit=20)
                precios[sc] = {
                    p["productId"]: p["items"][0]["sellers"][0]["commertialOffer"]["Price"]
                    for p in found
                }
        comunes = set(precios[1]) & set(precios[2])
        assert comunes, "No hubo productos en común para comparar canales"
        assert all(precios[1][pid] == precios[2][pid] for pid in comunes), (
            "El sales channel empezó a mover precios en DIA: revisar region_strategy"
        )

    async def test_dia_sigue_confirmando_su_catalogo_en_el_checkout(self):
        """El espejo del test de Disco: acá la simulación sí sirve, y de eso
        depende que `/basket/best-payment` mida el total de DIA en lugar de
        estimarlo."""
        async with _client(DIA_AR) as client:
            found = await client.search("leche", limit=1)
            sku = str(found[0]["items"][0]["itemId"])
            simulated = await client.simulate([(sku, 1)])

        items = simulated.get("items") or []
        assert items, "El checkout de DIA dejó de confirmar ítems"
        assert items[0].get("sellingPrice"), "El ítem volvió sin precio"

    async def test_disco_sigue_sin_exponer_regiones(self):
        """Si empieza a funcionar, podemos darle precios por sucursal."""
        async with _client(DISCO_AR) as client:
            assert await client.regions("1425") == []

    async def test_disco_sigue_rechazando_su_propio_catalogo_en_el_checkout(self):
        """Disco responde ORD027 para todo SKU, incluso con el sellerId que
        publica su propio catálogo. Por eso `supports_simulation=False`.

        Si esto empieza a funcionar, se puede confirmar la canasta en Disco y hay
        que sacar el flag."""
        async with _client(DISCO_AR) as client:
            found = await client.search("leche", limit=1)
            item = found[0]["items"][0]
            sku = str(item["itemId"])
            seller = str(item["sellers"][0]["sellerId"])

            simulated = await client.simulate([(sku, 1)], seller_id=seller)

        assert not simulated.get("items"), (
            "El checkout de Disco confirmó un ítem: revisar supports_simulation"
        )


class TestPromosPorMedioDePago:
    """El mecanismo del que depende todo el cálculo con tarjeta.

    Si algo de acá cambia, la app sigue devolviendo totales —plausibles y
    equivocados— hasta que alguien lo note en la caja del supermercado.
    """

    async def _un_sku_con_teaser_de_tarjeta(self, client) -> tuple[str, dict]:
        products = await client.search("leche", limit=20)
        for product in products:
            for item in product["items"]:
                for seller in item["sellers"]:
                    offer = seller["commertialOffer"]
                    for teaser in offer.get("PromotionTeasers") or []:
                        params = (teaser.get("Conditions") or {}).get("Parameters") or []
                        if any(p.get("Name") == "RestrictionsBins" for p in params):
                            return str(item["itemId"]), teaser
        pytest.skip("Ningún SKU de la muestra trae promo con BIN")

    async def test_carrefour_sigue_publicando_los_bin_que_califican(self):
        """`RestrictionsBins` es lo que permite saber qué tarjeta sirve.

        Sin este campo habría que adivinar el emisor por el nombre de la promo.
        """
        async with _client(CARREFOUR_AR) as client:
            _, teaser = await self._un_sku_con_teaser_de_tarjeta(client)

        bins = next(
            p["Value"]
            for p in teaser["Conditions"]["Parameters"]
            if p["Name"] == "RestrictionsBins"
        )
        assert bins, "el teaser trae RestrictionsBins vacío"
        assert all(part.strip().isdigit() for part in bins.split(","))

    async def test_el_bin_sigue_moviendo_el_precio_en_el_checkout(self):
        """Los dos lados de la prueba: un BIN que califica y uno que no.

        Que baje con el BIN bueno no alcanza —podría ser una promo general—; lo
        que confirma que el motor está mirando la tarjeta es que con el BIN malo
        NO baje.
        """
        async with _client(CARREFOUR_AR) as client:
            sku, teaser = await self._un_sku_con_teaser_de_tarjeta(client)
            valid_bin = next(
                p["Value"]
                for p in teaser["Conditions"]["Parameters"]
                if p["Name"] == "RestrictionsBins"
            ).split(",")[0].strip()

            plain = await client.simulate([(sku, 1)])
            with_card = await client.simulate([(sku, 1)], card_bin=valid_bin)
            with_other = await client.simulate([(sku, 1)], card_bin="450799")

        def selling(raw) -> int:
            return raw["items"][0]["sellingPrice"]

        assert selling(with_card) < selling(plain), (
            "El BIN dejó de activar el descuento: el cálculo con tarjeta quedó ciego"
        )
        assert selling(with_other) == selling(plain), (
            "Un BIN que no califica está activando descuento: el motor dejó de "
            "discriminar por tarjeta"
        )

    async def test_el_payment_system_sigue_siendo_obligatorio(self):
        """Mandar solo el `bin`, sin `paymentSystem`, no activa nada.

        Es la razón por la que `client.simulate` manda siempre un paymentSystem.
        Si esto cambiara, el campo pasaría a sobrar; mientras tanto, sacarlo
        rompe el cálculo en silencio.
        """
        async with _client(CARREFOUR_AR) as client:
            sku, teaser = await self._un_sku_con_teaser_de_tarjeta(client)
            valid_bin = next(
                p["Value"]
                for p in teaser["Conditions"]["Parameters"]
                if p["Name"] == "RestrictionsBins"
            ).split(",")[0].strip()

            plain = await client.simulate([(sku, 1)])
            body, _ = await request_json(
                client._client,
                "POST",
                "/api/checkout/pub/orderForms/simulation?sc=1",
                json={
                    "items": [{"id": sku, "quantity": 1, "seller": "1"}],
                    "country": "ARG",
                    "paymentData": {"payments": [{"bin": valid_bin}]},
                },
                limiter=client._limiter,
            )

        assert body["items"][0]["sellingPrice"] == plain["items"][0]["sellingPrice"], (
            "Un bin sin paymentSystem empezó a activar promos: se puede "
            "simplificar el payload de client.simulate"
        )

    async def test_el_seller_de_la_sucursal_sigue_apagando_las_promos(self):
        """Por esto `simulation_uses_store_seller=False`.

        Mandar el sellerId de `/regions` devuelve la canasta a precio de lista,
        con HTTP 200 y sin errores. El día que se arregle, el flag puede
        activarse y se gana precisión por sucursal.
        """
        async with _client(CARREFOUR_AR) as client:
            sku, teaser = await self._un_sku_con_teaser_de_tarjeta(client)
            valid_bin = next(
                p["Value"]
                for p in teaser["Conditions"]["Parameters"]
                if p["Name"] == "RestrictionsBins"
            ).split(",")[0].strip()

            regions = await client.regions("1425")
            seller = regions[0]["sellers"][0]["id"]

            default_seller = await client.simulate([(sku, 1)], card_bin=valid_bin)
            store_seller = await client.simulate(
                [(sku, 1)], seller_id=seller, card_bin=valid_bin
            )

        assert (
            store_seller["items"][0]["sellingPrice"]
            > default_seller["items"][0]["sellingPrice"]
        ), (
            "El seller de la sucursal dejó de romper las promos: revisar "
            "simulation_uses_store_seller, ahora se puede usar"
        )

    async def test_disco_sigue_sin_publicar_promos(self):
        """0 teasers en toda la muestra. Por eso sus promos se curan a mano.

        El día que empiece a publicarlas, hay que dejar de curarle reglas o se
        van a contar dos veces.
        """
        async with _client(DISCO_AR) as client:
            products = await client.search("leche", limit=20)

        teasers = [
            t
            for p in products
            for i in p["items"]
            for s in i["sellers"]
            for t in (
                (s["commertialOffer"].get("PromotionTeasers") or [])
                + (s["commertialOffer"].get("Teasers") or [])
            )
        ]
        assert not teasers, (
            f"Disco empezó a publicar promos ({len(teasers)}): revisar las reglas "
            "curadas para no contarlas dos veces"
        )


class TestFrescuraDelCatalogoCurado:
    """Las promos bancarias son un dato manual: envejecen sin avisar."""

    @pytest.mark.parametrize("rule", load_catalog().rules, ids=lambda r: r.id)
    async def test_ninguna_regla_esta_vencida_ni_sin_verificar_hace_mucho(self, rule):
        today = date.today()
        assert rule.valid_to >= today, (
            f"{rule.id} venció el {rule.valid_to}: sacala o renovale la vigencia"
        )
        antiguedad = (today - rule.verified_on).days
        assert antiguedad <= STALE_AFTER_DAYS, (
            f"{rule.id} no se verifica hace {antiguedad} días. Una promo vieja "
            f"manda a comprar donde no conviene: revisá {rule.source_url}"
        )
