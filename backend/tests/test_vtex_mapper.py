"""Normalización de VTEX a modelos canónicos, contra respuestas reales grabadas."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.models.catalog import PriceScope, PromotionKind, Store
from app.services.providers.vtex import mapper
from app.services.providers.vtex.stores import CARREFOUR_AR, DISCO_AR

CAPTURED_AT = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)


def _map(raw_products, config, store=None, scope=PriceScope.NATIONAL):
    out = []
    for raw in raw_products:
        out.extend(
            mapper.map_product_offers(
                raw, config, captured_at=CAPTURED_AT, store=store, scope=scope
            )
        )
    return out


class TestPrecioDeReferencia:
    def test_descarta_el_listprice_absurdo_de_disco(self, disco_search):
        """Disco publica ListPrice=252066 contra Price=3050.

        Tomarlo al pie de la letra mostraría un "98% de descuento" inexistente.
        """
        offers = _map(disco_search, DISCO_AR)
        kitkat = next(o for o in offers if "Kitkat" in o.product.name)
        assert kitkat.offer.price_cents == 305000
        assert kitkat.offer.reference_price_cents is None
        assert kitkat.offer.discount_percent is None

    def test_prefiere_price_without_discount_sobre_listprice(self, carrefour_search):
        """Carrefour: Price=10000.9, PriceWithoutDiscount=10990, ListPrice=15290.

        El descuento real es 9% (coincide con la promo "Dto de 9%"), no el 34%
        que saldría de usar ListPrice.
        """
        offers = _map(carrefour_search, CARREFOUR_AR)
        capsulas = next(o for o in offers if "Cápsulas" in o.product.name)
        assert capsulas.offer.price_cents == 1000090
        assert capsulas.offer.reference_price_cents == 1099000
        assert capsulas.offer.discount_percent == pytest.approx(9.0, abs=0.1)


class TestPromociones:
    def test_parsea_teasers_con_mangling_de_carrefour(self, carrefour_search):
        offers = _map(carrefour_search, CARREFOUR_AR)
        promos = [p for o in offers for p in o.offer.promotions]
        assert promos, "Carrefour publica promos; no se parseó ninguna"
        assert any(p.name == "Tarjeta Carrefour 15%" for p in promos)

    def test_clasifica_promo_de_tarjeta_como_medio_de_pago(self, carrefour_search):
        """Un 15% con tarjeta no es un descuento de góndola: no todos lo obtienen."""
        offers = _map(carrefour_search, CARREFOUR_AR)
        tarjeta = next(
            p for o in offers for p in o.offer.promotions if "Tarjeta" in p.name
        )
        assert tarjeta.kind is PromotionKind.PAYMENT_METHOD
        assert tarjeta.percent == 15.0

    def test_sin_promos_devuelve_tupla_vacia(self, disco_search):
        offers = _map(disco_search, DISCO_AR)
        kitkat = next(o for o in offers if "Kitkat" in o.product.name)
        assert kitkat.offer.promotions == ()


class TestAmbitoDelPrecio:
    def test_sin_sucursal_el_precio_es_nacional(self, disco_search):
        offers = _map(disco_search, DISCO_AR)
        assert all(o.offer.price_scope is PriceScope.NATIONAL for o in offers)
        assert all(o.offer.store_key is None for o in offers)

    def test_pedir_una_sucursal_no_alcanza_para_marcar_el_precio_como_local(
        self, carrefour_search
    ):
        """Regresión: antes bastaba con pasar una sucursal para etiquetar el
        precio como suyo, y así dos códigos postales distintos devolvían el mismo
        total con etiqueta de sucursal. El nivel lo decide el proveedor con
        `resolve_price_scope`, no el mapper."""
        store = Store(
            chain_slug="carrefour-ar",
            external_id="carrefourar0026",
            name="Hiper Warnes",
        )
        offers = _map(carrefour_search, CARREFOUR_AR, store=store)
        assert all(o.offer.price_scope is PriceScope.NATIONAL for o in offers)
        assert all(o.offer.store_key is None for o in offers)

    def test_solo_el_scope_store_nombra_la_sucursal(self, carrefour_search):
        store = Store(
            chain_slug="carrefour-ar",
            external_id="carrefourar0026",
            name="Hiper Warnes",
            sales_channel=1,
        )
        offers = _map(carrefour_search, CARREFOUR_AR, store=store, scope=PriceScope.STORE)
        assert all(o.offer.price_scope is PriceScope.STORE for o in offers)
        assert all(o.offer.store_key == "carrefour-ar:carrefourar0026" for o in offers)

    def test_un_canal_elegido_no_es_un_precio_de_sucursal(self, carrefour_search):
        """`CHANNEL` es un precio real y distinto, pero de un formato de tienda,
        no de tu sucursal: no puede llevar su nombre."""
        store = Store(
            chain_slug="carrefour-ar", external_id="carrefourar0026", name="Hiper Warnes"
        )
        offers = _map(
            carrefour_search, CARREFOUR_AR, store=store, scope=PriceScope.CHANNEL
        )
        assert all(o.offer.price_scope is PriceScope.CHANNEL for o in offers)
        assert all(o.offer.store_key is None for o in offers)


class TestProducto:
    def test_extrae_ean_normalizado(self, carrefour_search):
        offers = _map(carrefour_search, CARREFOUR_AR)
        eans = [o.product.ean for o in offers if o.product.ean]
        assert eans, "No se extrajo ningún EAN"
        assert all(len(e) == 13 and e.isdigit() for e in eans)

    def test_extrae_la_categoria_mas_profunda(self, carrefour_search):
        offers = _map(carrefour_search, CARREFOUR_AR)
        capsulas = next(o for o in offers if "Cápsulas" in o.product.name)
        assert capsulas.product.category_path == (
            "Desayuno y merienda", "Café", "Cápsulas de café",
        )

    def test_descarta_skus_sin_precio(self):
        """Cuando una sucursal no vende un SKU, el item llega igual pero sin
        precio. Sin filtrarlo aparecería como un producto a $0."""
        raw = {
            "productId": "1",
            "productName": "Fantasma",
            "items": [{
                "itemId": "10",
                "sellers": [{"sellerId": "1", "sellerDefault": True,
                             "commertialOffer": {"Price": 0, "AvailableQuantity": 0}}],
            }],
        }
        assert mapper.map_product_offers(raw, DISCO_AR, captured_at=CAPTURED_AT) == []


class TestSucursales:
    def test_mapea_regiones_a_sucursales(self, carrefour_regions):
        stores = mapper.map_regions_to_stores(carrefour_regions, CARREFOUR_AR, "1425")
        assert any(s.name == "Hiper Warnes" for s in stores)
        assert all(s.postal_code == "1425" for s in stores)
        assert all(s.region_id for s in stores)

    def test_da_nombre_legible_a_sucursales_sin_nombre(self, carrefour_regions):
        """Algunas sucursales repiten el id como nombre ('carrefourar0137')."""
        stores = mapper.map_regions_to_stores(carrefour_regions, CARREFOUR_AR, "1425")
        anonima = next(s for s in stores if s.external_id == "carrefourar0137")
        assert anonima.name == "Sucursal carrefourar0137"

    def test_no_duplica_sucursales_entre_regiones(self, carrefour_regions):
        stores = mapper.map_regions_to_stores(carrefour_regions, CARREFOUR_AR, "1425")
        assert len({s.external_id for s in stores}) == len(stores)


class TestArbolDeCategorias:
    def test_acumula_el_path_completo(self, carrefour_category_tree):
        """`fq=C:` exige el path raíz->nodo; con el id suelto devuelve 0."""
        tree = mapper.map_category_tree(carrefour_category_tree, CARREFOUR_AR)
        raiz = next(n for n in tree if n.name == "Desayuno y merienda")
        assert raiz.id_path == (raiz.id,)
        assert raiz.fq == f"C:/{raiz.id}/"
        hijo = raiz.children[0]
        assert hijo.id_path == (raiz.id, hijo.id)
        assert hijo.fq == f"C:/{raiz.id}/{hijo.id}/"

    def test_excluye_las_categorias_basura(self, carrefour_category_tree):
        """Carrefour publica una 'Test Category' con id 1."""
        tree = mapper.map_category_tree(carrefour_category_tree, CARREFOUR_AR)
        assert all(n.id != 1 for n in tree)
        assert not any(n.name == "Test Category" for n in tree)
