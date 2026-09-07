"""Normalización de Constructor.io a modelos canónicos, contra respuestas reales.

El grueso de estos tests defiende una sola idea: en la API de Coto los nombres
de los campos no significan lo que parecen, y creerles publica precios
equivocados. Cada test nombra el número concreto que se rompería.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from app.models.catalog import PriceScope, PromotionKind, Store
from app.services.providers.coto import mapper
from app.services.providers.coto.config import COTO_AR

CAPTURED_AT = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
FIXTURES = Path(__file__).parent / "fixtures" / "coto"


def load(name: str) -> list[dict[str, Any]]:
    payload = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    return payload["response"]["results"]


def load_groups(name: str) -> list[dict[str, Any]]:
    payload = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    return payload["response"]["groups"]


@pytest.fixture
def leche() -> list[dict[str, Any]]:
    return load("search_leche")


@pytest.fixture
def aceite() -> list[dict[str, Any]]:
    return load("search_aceite")


def _map(results, store=None, store_id=None, drop_unmatched=False):
    return mapper.map_product_offers(
        results,
        COTO_AR,
        captured_at=CAPTURED_AT,
        store=store,
        store_id=store_id,
        drop_unmatched=drop_unmatched,
    )


def _by_name(offers, fragment: str):
    return next(o for o in offers if fragment.lower() in o.product.name.lower())


class TestPrecioDeGondola:
    """`formatPrice` no es el precio de venta, aunque el nombre invite."""

    def test_el_precio_es_listprice_y_no_formatprice(self, aceite):
        """Aceite Cañuelas 1,5 L: listPrice=6530, formatPrice=4353,34.

        Es el caso más peligroso porque `formatPrice` es *más barato*: tomarlo
        como precio no rompe nada visible, sólo hace que Coto gane comparaciones
        que no gana, con un precio que en la caja no existe.
        """
        offer = _by_name(_map(aceite), "Cañuelas").offer
        assert offer.price_cents == 653000
        assert offer.price_per_unit_cents == 435334

    def test_no_infla_el_precio_de_los_envases_chicos(self, leche):
        """En envases de menos de un litro `formatPrice` es *mayor*.

        Leche La Serenísima 1 L: ambos coinciden en 2999 porque el envase es de
        un litro justo. La regresión que importa es que el precio salga del
        campo correcto, no que los dos números coincidan por casualidad.
        """
        offer = _by_name(_map(leche), "Protein")
        assert offer.offer.price_cents == 299900

    def test_el_precio_por_unidad_queda_disponible_para_comparar(self, aceite):
        """Es la función central del producto: 1,5 L contra 900 ml."""
        offer = _by_name(_map(aceite), "NATURA").offer
        assert offer.price_cents == 697500
        assert offer.price_per_unit_cents == 465000


class TestSucursal133:
    """La sucursal 133 publica `formatPrice` roto y hay que sobrevivirlo."""

    def test_descarta_el_precio_por_unidad_absurdo(self, aceite):
        """Aceite NATURA 1,5 L en la 133: listPrice=6329, formatPrice=29,46.

        29,46 por litro para un aceite de $6.329 es imposible (sería 0,5% del
        precio). Se descarta el campo, no la oferta.
        """
        offer = _by_name(_map(aceite, store_id="133"), "NATURA").offer
        assert offer.price_cents == 632900, "el precio de góndola sí es utilizable"
        assert offer.price_per_unit_cents is None

    def test_conserva_el_precio_por_unidad_cuando_es_plausible(self, aceite):
        """La misma sucursal, otro producto: 5925 / 1,5 = 3948,99. Ese sí vale."""
        offer = _by_name(_map(aceite, store_id="133"), "Cañuelas").offer
        assert offer.price_cents == 592500
        assert offer.price_per_unit_cents == 394899


class TestPromociones:
    def test_la_promo_por_cantidad_no_baja_el_precio_unitario(self, leche):
        """Leche Protein: "50% 2da" llevando 2, discountPrice=$2249,25.

        $2249,25 es el promedio pagando por dos unidades. Aplicarlo a una sola
        inventa un 25% que en la caja no existe —y como esta canasta compite
        contra Carrefour y Disco por el total más barato, ese descuento
        inventado le haría ganar a Coto una comparación que no gana.
        """
        offer = _by_name(_map(leche), "Protein").offer
        assert offer.price_cents == 299900
        assert offer.reference_price_cents is None
        assert offer.discount_percent is None

    def test_pero_declara_la_promo_por_cantidad(self, leche):
        promo = next(
            p
            for p in _by_name(_map(leche), "Protein").offer.promotions
            if p.kind is PromotionKind.BUY_X_GET_Y
        )
        assert promo.minimum_quantity == 2
        # El 50 es sobre la segunda unidad, no sobre la compra: publicarlo como
        # porcentaje del total duplicaría el ahorro real.
        assert promo.percent is None

    def test_el_descuento_directo_si_baja_el_precio(self, leche):
        """NESQUIK: "40%Dto" sin condición, listPrice=5100, discountPrice=3060.

        Acá el descuento se paga por una unidad suelta, así que el precio real
        es 3060 y el de góndola pasa a ser el tachado.
        """
        offer = _by_name(_map(leche), "NESQUIK").offer
        assert offer.price_cents == 306000
        assert offer.reference_price_cents == 510000
        assert offer.discount_percent == pytest.approx(40.0, abs=0.1)

    def test_confia_en_el_precio_publicado_y_no_en_el_porcentaje(self, aceite):
        """Aceite Pureza: "25%Dto" sobre 6980 da 5235, pero Coto publica 5234,30.

        Recalcular el precio desde el porcentaje introduce una diferencia de 70
        centavos contra lo que la cadena realmente cobra.
        """
        offer = _by_name(_map(aceite), "PUREZA").offer
        assert offer.price_cents == 523430

    def test_las_promos_de_tarjeta_no_tocan_el_precio(self, leche):
        """Un descuento con tarjeta no lo obtienen todos: es promo, no precio."""
        offer = _by_name(_map(leche), "Protein").offer
        assert any(p.kind is PromotionKind.PAYMENT_METHOD for p in offer.promotions)
        assert offer.price_cents == 299900

    def test_el_producto_sin_promo_no_inventa_ninguna(self, leche):
        offer = _by_name(_map(leche), "Entera COTO").offer
        assert offer.promotions == ()
        assert offer.reference_price_cents is None


class TestProducto:
    def test_normaliza_el_ean_que_viene_como_numero(self, leche):
        """Coto serializa `product_main_ean` sin comillas: 7790742358608."""
        product = _by_name(_map(leche), "Protein").product
        assert product.ean == "7790742358608"

    def test_mapea_identificadores_marca_e_imagen(self, leche):
        product = _by_name(_map(leche), "Protein").product
        assert product.product_id == "prod00508911"
        assert product.sku_id == "sku00508911"
        assert product.brand == "LA SERENISIMA"
        assert product.image_url and product.image_url.startswith("https://")
        assert product.chain_slug == "coto-ar"

    def test_arma_el_camino_de_categorias_mas_especifico(self, leche):
        path = _by_name(_map(leche), "Protein").product.category_path
        assert path[:2] == ("Frescos", "Lácteos")
        assert "Categorias" not in path, "la raíz sintética no es una categoría"

    def test_el_link_apunta_al_sitio_de_coto(self, leche):
        link = _by_name(_map(leche), "Protein").product.link
        assert link and link.startswith("https://www.cotodigital.com.ar/")


class TestAmbitoDePrecio:
    def test_sin_sucursal_el_precio_es_nacional(self, leche):
        offer = _by_name(_map(leche), "Protein").offer
        assert offer.price_scope is PriceScope.NATIONAL
        assert offer.store_key is None

    def test_con_sucursal_declara_precio_de_sucursal(self, leche):
        """Coto es la primera cadena soportada que puede dar STORE de verdad."""
        store = Store(chain_slug="coto-ar", external_id="133", name="Sucursal 133")
        offer = _by_name(_map(leche, store=store, store_id="133"), "Liviana").offer
        assert offer.price_scope is PriceScope.STORE
        assert offer.store_key == "coto-ar:133"
        assert offer.seller_id == "133"

    def test_cae_a_nacional_si_la_sucursal_no_publica_ese_producto(self, aceite):
        """Y **no** lo etiqueta como precio de sucursal.

        El Pureza no tiene entrada para la 133. Devolver el precio de referencia
        diciendo que es el de tu sucursal es justo lo que `PriceScope` existe
        para evitar.
        """
        store = Store(chain_slug="coto-ar", external_id="133", name="Sucursal 133")
        offer = _by_name(_map(aceite, store=store, store_id="133"), "PUREZA").offer
        assert offer.price_scope is PriceScope.NATIONAL
        assert offer.store_key is None
        assert offer.price_cents == 523430

    def test_la_disponibilidad_sale_de_store_availability(self, leche):
        offer = _by_name(_map(leche), "Protein").offer
        assert offer.available is True


class TestFallbackSemantico:
    """Constructor.io devuelve cualquier cosa cuando no hay match léxico."""

    def test_descarta_los_resultados_sin_termino_matcheado(self):
        """`zzzqqxnotaproduct` devuelve remeras con HTTP 200.

        Sin este filtro, buscar "leche" mezcla ropa de Coto con la leche de
        Carrefour en la comparación federada.
        """
        raw = load("search_sin_match")
        assert raw, "la fixture debería tener el fallback semántico grabado"
        assert _map(raw, drop_unmatched=True) == []

    def test_sin_el_filtro_esos_resultados_pasarian(self):
        raw = load("search_sin_match")
        assert _map(raw, drop_unmatched=False), "los resultados existen; el filtro es lo que los saca"

    def test_no_descarta_resultados_legitimos(self, leche):
        assert len(_map(leche, drop_unmatched=True)) == len(_map(leche))


class TestBusquedaPorEan:
    def test_la_busqueda_por_ean_devuelve_ese_producto(self):
        offers = _map(load("search_ean"))
        assert offers
        assert offers[0].product.ean == "7790742333605"


class TestArbolDeCategorias:
    def test_mapea_el_arbol_saltando_la_raiz_sintetica(self):
        tree = mapper.map_category_tree(load_groups("category_tree"))
        assert tree, "la faceta de grupos debería dar categorías"
        assert all(node.name != "Categorias" for node in tree)

    def test_acumula_el_camino_de_ids(self):
        tree = mapper.map_category_tree(load_groups("category_tree"))
        parent = next(n for n in tree if n.children)
        child = parent.children[0]
        assert child.id_path == (*parent.id_path, child.id)

    @pytest.mark.parametrize(
        ("group_id", "numero"),
        [("catv00001255", 1255), ("catv00003266", 3266), ("categoria", None)],
    )
    def test_convierte_el_group_id_a_entero(self, group_id, numero):
        assert mapper.group_id_to_int(group_id) == numero

    def test_la_conversion_de_group_id_es_reversible(self):
        """Hace falta para volver a pedirle la categoría a la API."""
        assert mapper.int_to_group_id(1255) == "catv00001255"
        assert mapper.group_id_to_int(mapper.int_to_group_id(3266)) == 3266


class TestRobustez:
    def test_saltea_los_resultados_sin_precio(self):
        assert _map([{"data": {"id": "prod1", "price": []}}]) == []

    def test_saltea_los_resultados_sin_id(self):
        assert _map([{"data": {"price": [{"store": "200", "listPrice": 10}]}}]) == []

    def test_saltea_el_precio_en_cero(self):
        raw = [{"data": {"id": "p", "price": [{"store": "200", "listPrice": 0}]}}]
        assert _map(raw) == []

    def test_tolera_resultados_malformados(self):
        assert _map(["no-es-un-dict", {}, {"data": None}]) == []

    def test_usa_product_list_price_si_falta_la_sucursal_de_referencia(self):
        raw = [{"data": {"id": "p", "price": [], "product_list_price": 1500}}]
        offer = _map(raw)[0].offer
        assert offer.price_cents == 150000
        assert offer.price_scope is PriceScope.NATIONAL

    @pytest.mark.parametrize(
        ("texto", "esperado"),
        [("$2249.25", 2249.25), ("Precio Contado: $2999", 2999.0), ("", None), (None, None)],
    )
    def test_parsea_los_importes_de_las_promos(self, texto, esperado):
        assert mapper.parse_money(texto) == esperado
