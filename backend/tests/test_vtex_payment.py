"""Cómo se le pide a VTEX el precio con tarjeta.

Todo acá se apoya en comportamiento medido contra Carrefour. Los dos casos que
más importan no fallan con un error: devuelven un total plausible y equivocado.
"""

from __future__ import annotations

import json
from dataclasses import replace

import httpx
import pytest
import respx

from app.models.catalog import Store
from app.services.providers.vtex.client import VTEXClient
from app.services.providers.vtex.provider import VTEXProvider
from app.services.providers.vtex.stores import CARREFOUR_AR, DISCO_AR

SIMULATION_URL = f"{CARREFOUR_AR.base_url}/api/checkout/pub/orderForms/simulation"


def _response(total: int = 100_000, discount: int = 0, promos: list[str] | None = None):
    return httpx.Response(
        200,
        json={
            "items": [
                {"id": "52726", "quantity": 1, "sellingPrice": total - discount}
            ],
            "totals": [
                {"id": "Items", "name": "Total de los items", "value": total},
                *(
                    [{"id": "Discounts", "name": "Total de descuentos", "value": -discount}]
                    if discount
                    else []
                ),
            ],
            "ratesAndBenefitsData": {
                "rateAndBenefitsIdentifiers": [
                    {"name": name} for name in (promos or [])
                ]
            },
        },
    )


def _provider(config=CARREFOUR_AR):
    return VTEXProvider(config, VTEXClient(config, user_agent="test"))


def _sent(route) -> dict:
    return json.loads(route.calls[0].request.content)


@pytest.mark.asyncio
class TestPayloadDeTarjeta:
    async def test_sin_bin_no_se_manda_payment_data(self):
        async with VTEXClient(CARREFOUR_AR, user_agent="test") as client:
            with respx.mock(base_url=CARREFOUR_AR.base_url) as mock:
                route = mock.post("/api/checkout/pub/orderForms/simulation").mock(
                    return_value=_response()
                )
                await client.simulate([("52726", 1)])
        assert "paymentData" not in _sent(route)

    async def test_con_bin_manda_payment_system_ademas_del_bin(self):
        """El `paymentSystem` es obligatorio aunque su valor sea indiferente.

        Medido: `paymentData` con `bin` pero sin `paymentSystem` no activa
        ninguna promo y devuelve el precio de góndola. El fallo es silencioso —
        HTTP 200, canasta completa— así que si alguien saca ese campo, esto es lo
        único que lo va a notar.
        """
        async with VTEXClient(CARREFOUR_AR, user_agent="test") as client:
            with respx.mock(base_url=CARREFOUR_AR.base_url) as mock:
                route = mock.post("/api/checkout/pub/orderForms/simulation").mock(
                    return_value=_response()
                )
                await client.simulate([("52726", 1)], card_bin="507858")

        payment = _sent(route)["paymentData"]["payments"][0]
        assert payment["bin"] == "507858"
        assert payment["paymentSystem"], "sin paymentSystem el motor de promos no corre"


@pytest.mark.asyncio
class TestSellerDeLaSucursal:
    """La regresión más cara del módulo.

    Mandar el `sellerId` que devuelve `/regions` (`carrefourar0026`) en lugar del
    seller por defecto **apaga el motor de promociones**: la misma canasta con el
    mismo BIN pasó de $17.922,25 con "Tarjeta Carrefour 15%" aplicada a $21.085
    sin ninguna promo, con HTTP 200 y todos los ítems confirmados.
    """

    @pytest.fixture
    def warnes(self):
        return Store(
            chain_slug="carrefour-ar",
            external_id="carrefourar0026",
            name="Hiper Warnes",
            postal_code="1425",
        )

    async def test_no_usa_el_seller_de_la_sucursal_por_defecto(self, warnes):
        provider = _provider()
        with respx.mock(base_url=CARREFOUR_AR.base_url) as mock:
            route = mock.post("/api/checkout/pub/orderForms/simulation").mock(
                return_value=_response()
            )
            await provider.simulate_basket([("52726", 1)], store=warnes)
        assert _sent(route)["items"][0]["seller"] == "1"

    async def test_igual_manda_el_codigo_postal_de_la_sucursal(self, warnes):
        """El seller rompe, el código postal no: sirve para regionalizar."""
        provider = _provider()
        with respx.mock(base_url=CARREFOUR_AR.base_url) as mock:
            route = mock.post("/api/checkout/pub/orderForms/simulation").mock(
                return_value=_response()
            )
            await provider.simulate_basket([("52726", 1)], store=warnes)
        assert _sent(route)["postalCode"] == "1425"

    async def test_una_cadena_que_lo_declare_si_lo_usa(self, warnes):
        config = replace(CARREFOUR_AR, simulation_uses_store_seller=True)
        provider = _provider(config)
        with respx.mock(base_url=config.base_url) as mock:
            route = mock.post("/api/checkout/pub/orderForms/simulation").mock(
                return_value=_response()
            )
            await provider.simulate_basket([("52726", 1)], store=warnes)
        assert _sent(route)["items"][0]["seller"] == "carrefourar0026"

    async def test_verify_prices_usa_el_mismo_criterio(self, warnes):
        """`/basket/verify` perdía las promos por lo mismo."""
        provider = _provider()
        with respx.mock(base_url=CARREFOUR_AR.base_url) as mock:
            route = mock.post("/api/checkout/pub/orderForms/simulation").mock(
                return_value=_response()
            )
            await provider.verify_prices([("52726", 1)], store=warnes)
        assert _sent(route)["items"][0]["seller"] == "1"


@pytest.mark.asyncio
class TestSimulacionDeCanasta:
    async def test_lee_el_total_de_items_y_descuentos(self):
        provider = _provider()
        with respx.mock(base_url=CARREFOUR_AR.base_url) as mock:
            mock.post("/api/checkout/pub/orderForms/simulation").mock(
                return_value=_response(
                    total=2_108_500, discount=421_700, promos=["Cuenta Digital 20%"]
                )
            )
            result = await provider.simulate_basket([("52726", 1)], card_bin="223364")

        assert result is not None
        assert result.items_total_cents == 2_108_500
        assert result.discount_cents == 421_700
        assert result.total_cents == 1_686_800
        assert [p.name for p in result.promotions] == ["Cuenta Digital 20%"]
        assert result.card_bin == "223364"

    async def test_ignora_el_envio_al_totalizar(self):
        """El checkout devuelve más totales que mercadería; sumarlos todos haría
        que el envío se cuente como si fuera producto."""
        provider = _provider()
        with respx.mock(base_url=CARREFOUR_AR.base_url) as mock:
            mock.post("/api/checkout/pub/orderForms/simulation").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "items": [{"id": "52726", "sellingPrice": 100_000}],
                        "totals": [
                            {"id": "Items", "value": 100_000},
                            {"id": "Discounts", "value": -10_000},
                            {"id": "Shipping", "value": 500_000},
                        ],
                    },
                )
            )
            result = await provider.simulate_basket([("52726", 1)])
        assert result is not None
        assert result.total_cents == 90_000

    async def test_un_sku_sin_precio_queda_como_faltante(self):
        """Viene el ítem pero sin `sellingPrice`: la sucursal no lo vende.

        Contarlo como confirmado daría un total al que le falta un producto, más
        barato justamente por estar incompleto.
        """
        provider = _provider()
        with respx.mock(base_url=CARREFOUR_AR.base_url) as mock:
            mock.post("/api/checkout/pub/orderForms/simulation").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "items": [
                            {"id": "52726", "sellingPrice": 100_000},
                            {"id": "99999"},
                        ],
                        "totals": [{"id": "Items", "value": 100_000}],
                    },
                )
            )
            result = await provider.simulate_basket([("52726", 1), ("99999", 1)])

        assert result is not None
        assert result.missing_sku_ids == frozenset({"99999"})
        assert result.complete is False

    async def test_disco_devuelve_none_en_vez_de_fallar(self):
        """No tiene checkout consultable. Es una respuesta legítima."""
        provider = _provider(DISCO_AR)
        assert await provider.simulate_basket([("238230", 1)]) is None

    async def test_una_canasta_vacia_no_llama_a_la_red(self):
        provider = _provider()
        with respx.mock(base_url=CARREFOUR_AR.base_url, assert_all_called=False) as mock:
            route = mock.post("/api/checkout/pub/orderForms/simulation")
            assert await provider.simulate_basket([]) is None
        assert not route.called


class TestContraRespuestasReales:
    """Contra las dos simulaciones grabadas del mismo SKU, con y sin tarjeta.

    Se afirman invariantes y no importes: la promo vigente cambia por día —la
    grabación pasó de "Mi CRF 9%" a "Tarjeta Carrefour 20% Off Martes" entre dos
    corridas— así que fijar el número haría fallar el test cada vez que se
    regraban las fixtures, por un motivo que no es un bug.
    """

    def test_la_tarjeta_baja_el_precio(
        self, carrefour_simulation_no_card, carrefour_simulation_bin
    ):
        sin_tarjeta = carrefour_simulation_no_card["items"][0]["sellingPrice"]
        con_tarjeta = carrefour_simulation_bin["items"][0]["sellingPrice"]
        assert con_tarjeta < sin_tarjeta

    def test_la_tarjeta_activa_una_promo_distinta(
        self, carrefour_simulation_no_card, carrefour_simulation_bin
    ):
        """El motor elige una promo por ítem: la de la tarjeta desplaza a la que
        estaba, no se suma. Es la razón por la que el ahorro se mide restando dos
        simulaciones en vez de aplicar un porcentaje."""

        def promos(raw):
            return {
                p["name"]
                for p in raw["ratesAndBenefitsData"]["rateAndBenefitsIdentifiers"]
            }

        assert promos(carrefour_simulation_bin) != promos(carrefour_simulation_no_card)

    def test_el_mapper_lee_bien_la_respuesta_grabada(
        self, carrefour_simulation_no_card, carrefour_simulation_bin
    ):
        from app.services.providers.vtex.provider import _applied_promotions, _totals

        items, discount = _totals(carrefour_simulation_bin)
        assert items > 0
        assert discount > 0
        assert items - discount == carrefour_simulation_bin["items"][0]["sellingPrice"]
        assert _applied_promotions(carrefour_simulation_bin)
