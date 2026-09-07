"""Depuración de candidatos: filtro por nombre y filtro de outliers de precio.

Ambos son funciones puras, así que los candidatos se arman a mano en el orden de
relevancia que devolvería la cadena.

Los casos no son inventados: son las formas concretas en las que la búsqueda de
una cadena ensucia la comparación —la chocolatada en la búsqueda de leche, el
sachet colado entre los litros, el pack de 6 en la del envase suelto—.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import pytest

from app.models.catalog import Offer, PriceScope, Product, ProductOffer
from app.services.matching import select_candidate
from app.services.matching.names import filter_by_name, normalize, score_name
from app.services.matching.outliers import flag_price_outliers

T0 = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)

CARREFOUR = "carrefour-ar"


def offer(
    name: str,
    price_cents: int = 100_000,
    *,
    brand: str | None = None,
    chain: str = CARREFOUR,
    ean: str | None = None,
) -> ProductOffer:
    return ProductOffer(
        product=Product(
            chain_slug=chain,
            product_id=f"p-{name}",
            sku_id=f"s-{name}",
            name=name,
            brand=brand,
            ean=ean,
        ),
        offer=Offer(
            price_cents=price_cents,
            available=True,
            captured_at=T0,
            price_scope=PriceScope.NATIONAL,
        ),
    )


class TestNormalizacion:
    def test_saca_acentos_y_puntuacion(self):
        assert normalize("Leche La Serenísima 1,5 Lt.") == [
            "leche",
            "la",
            "serenisima",
            "1",
            "5",
            "lt",
        ]

    def test_separa_numero_de_unidad(self):
        """'1l' y '1 Lt' tienen que ser la misma cosa: el envase suele ser lo
        único que distingue dos presentaciones del mismo producto."""
        assert normalize("1l") == ["1", "l"]
        assert normalize("900ml") == ["900", "ml"]


class TestScoreDeNombre:
    def test_el_ruido_de_gondola_no_castiga_al_candidato_correcto(self):
        """El nombre de góndola trae palabras que el término nunca va a tener.
        Una métrica simétrica castigaría al producto correcto por verboso."""
        score = score_name(
            "coca cola 2.25",
            offer("Gaseosa Coca-Cola Sabor Original 2.25 Lt").product,
        )
        assert score == pytest.approx(100.0)

    def test_falta_un_dato_del_termino_y_el_score_cae(self):
        assert (
            score_name("yerba playadito 1kg", offer("Yerba Mate Union 1 Kg").product)
            < 85
        )

    def test_la_marca_cuenta_aunque_no_este_en_el_nombre(self):
        """Varias cadenas dejan la marca fuera del nombre. Sin mirarla, un
        término que la nombra —lo normal en una lista— puntuaría bajo contra el
        producto correcto."""
        sin_marca = offer("Yerba Mate Con Palo 1 Kg").product
        con_marca = offer("Yerba Mate Con Palo 1 Kg", brand="Playadito").product

        assert score_name("yerba playadito 1kg", sin_marca) < 85
        assert score_name("yerba playadito 1kg", con_marca) >= 85

    def test_las_palabras_cortas_pesan_menos_que_las_largas(self):
        """Perder 'de' no puede valer lo mismo que perder 'girasol'."""
        sin_de = score_name(
            "aceite de girasol natura", offer("Aceite Girasol Natura 900 Ml").product
        )
        sin_girasol = score_name(
            "aceite de girasol natura", offer("Aceite De Oliva Natura 500 Ml").product
        )
        assert sin_de > 90  # sigue holgadamente sobre el umbral
        assert sin_girasol < 85
        assert sin_de - sin_girasol > 15


class TestFiltroPorNombre:
    def test_saca_la_chocolatada_de_la_busqueda_de_leche(self):
        candidatos = [
            offer("Leche Entera La Serenisima 1 Lt"),
            offer("Leche Chocolatada Cindor 200 Ml"),
            offer("Dulce De Leche La Serenisima 400 Gr"),
        ]
        resultado = filter_by_name("leche entera la serenisima 1l", candidatos)

        assert [o.product.name for o in resultado.offers] == [
            "Leche Entera La Serenisima 1 Lt"
        ]
        assert len(resultado.discarded) == 2

    def test_respeta_el_orden_de_relevancia_de_la_cadena(self):
        """Ordenar por puntaje sería reemplazar el criterio de la cadena por uno
        propio; el comparador usa ese orden para desempatar."""
        candidatos = [
            offer("Fideos Guiseros Tirabuzon Matarazzo 500 Gr"),
            offer("Fideos Tirabuzon Matarazzo 500 Gr"),
        ]
        resultado = filter_by_name("fideos matarazzo tirabuzon 500", candidatos)

        assert [o.product.name for o in resultado.offers] == [
            "Fideos Guiseros Tirabuzon Matarazzo 500 Gr",
            "Fideos Tirabuzon Matarazzo 500 Gr",
        ]

    def test_si_nada_llega_al_umbral_no_se_queda_con_el_menos_malo(self):
        """Un mal match no se vuelve bueno por ser el mejor disponible."""
        resultado = filter_by_name(
            "yerba playadito 1kg", [offer("Yerba Mate Union 1 Kg")]
        )

        assert resultado.offers == []
        assert resultado.emptied
        assert resultado.best_discarded is not None

    def test_avisa_cuando_vacia_la_lista_y_dice_con_cuanto(self, caplog):
        """El aviso trae el mejor puntaje descartado: es el número con el que se
        calibra el umbral."""
        with caplog.at_level(logging.WARNING, logger="app.services.matching.names"):
            filter_by_name("yerba playadito 1kg", [offer("Yerba Mate Union 1 Kg")])

        assert "descartó los 1 candidatos" in caplog.text
        assert "Yerba Mate Union 1 Kg" in caplog.text

    def test_el_umbral_es_configurable_por_llamada(self):
        candidatos = [offer("Leche Descremada La Serenisima 1 Lt")]
        termino = "leche entera la serenisima 1l"

        assert filter_by_name(termino, candidatos, min_score=85).offers == []
        assert filter_by_name(termino, candidatos, min_score=70).offers != []

    def test_termino_de_una_palabra_no_distingue_variedades(self):
        """Límite conocido, documentado acá para que no sorprenda: 'Leche Entera'
        y 'Leche Chocolatada' contienen los dos el término entero. Ningún umbral
        los separa; contra eso siguen valiendo el EAN y el orden de relevancia."""
        resultado = filter_by_name(
            "leche",
            [offer("Leche Entera La Serenisima 1 Lt"), offer("Leche Chocolatada Cindor 200 Ml")],
        )
        assert len(resultado.offers) == 2


class TestOutliersDePrecio:
    def test_descarta_el_precio_despegado_de_la_mediana(self):
        candidatos = [
            offer("Leche Entera La Serenisima 1 Lt", 200_000),
            offer("Leche Entera La Serenisima 1 Lt Pack", 210_000),
            offer("Leche Entera La Serenisima Sachet", 90_000),
        ]
        resultado = flag_price_outliers("leche entera", candidatos)

        assert resultado.enforced
        assert [o.product.name for o in resultado.offers] == [
            "Leche Entera La Serenisima 1 Lt",
            "Leche Entera La Serenisima 1 Lt Pack",
        ]
        assert [p.name for p in resultado.outliers] == [
            "Leche Entera La Serenisima Sachet"
        ]

    def test_la_mediana_no_se_deja_arrastrar_por_el_outlier(self):
        """Con promedio, el barato bajaría el centro y dejaría de parecer raro."""
        candidatos = [
            offer("A", 200_000),
            offer("B", 200_000),
            offer("C", 200_000),
            offer("D", 20_000),
        ]
        resultado = flag_price_outliers("x", candidatos)

        assert resultado.median_cents == 200_000
        assert [p.name for p in resultado.outliers] == ["D"]

    def test_loguea_producto_precio_y_desvio(self, caplog):
        """El log es el entregable del filtro: sin producto, precio y porcentaje
        no hay nada que revisar después."""
        candidatos = [offer("Normal A", 100_000), offer("Normal B", 100_000), offer("Caro", 200_000)]
        with caplog.at_level(logging.WARNING, logger="app.services.matching.outliers"):
            flag_price_outliers("leche", candidatos)

        assert "Caro" in caplog.text
        assert "$2,000.00" in caplog.text
        assert "+100.0%" in caplog.text
        assert "descartado" in caplog.text

    def test_un_solo_candidato_nunca_se_descarta(self):
        """No hay con qué compararlo: descartarlo dejaría la línea sin resolver
        por una sospecha que no se puede sostener."""
        resultado = flag_price_outliers(
            "leche", [offer("Leche Rara", 500_000)], reference_median_cents=100_000
        )

        assert len(resultado.offers) == 1
        assert not resultado.enforced

    def test_un_solo_candidato_desviado_igual_se_avisa(self, caplog):
        with caplog.at_level(logging.WARNING, logger="app.services.matching.outliers"):
            flag_price_outliers(
                "leche", [offer("Leche Rara", 500_000)], reference_median_cents=100_000
            )

        assert "+400.0%" in caplog.text
        assert "se conserva" in caplog.text

    def test_con_dos_candidatos_no_descarta(self):
        """La mediana de dos es el punto medio: los dos se desvían lo mismo en
        direcciones opuestas y no se aprende cuál está mal."""
        resultado = flag_price_outliers(
            "leche", [offer("A", 100_000), offer("B", 300_000)]
        )

        assert len(resultado.offers) == 2
        assert not resultado.enforced
        assert len(resultado.outliers) == 2

    def test_si_todos_se_desvian_se_conservan_todos(self):
        """Un grupo partido en dos mitades simétricas no tiene centro creíble;
        vaciarlo sería descartar por no poder decidir."""
        candidatos = [
            offer("A", 100_000),
            offer("B", 100_000),
            offer("C", 300_000),
            offer("D", 300_000),
        ]
        resultado = flag_price_outliers("x", candidatos)

        assert len(resultado.offers) == 4
        assert not resultado.enforced
        assert len(resultado.outliers) == 4

    def test_la_tolerancia_es_configurable(self):
        candidatos = [offer("A", 100_000), offer("B", 100_000), offer("C", 125_000)]

        assert len(flag_price_outliers("x", candidatos, tolerance_pct=20).offers) == 2
        assert len(flag_price_outliers("x", candidatos, tolerance_pct=30).offers) == 3

    def test_precio_cero_no_rompe_el_porcentaje(self):
        candidatos = [offer("A", 0), offer("B", 0), offer("C", 0)]
        resultado = flag_price_outliers("x", candidatos)

        assert len(resultado.offers) == 3
        assert not resultado.enforced


class TestSeleccionCompleta:
    def test_gana_el_mas_barato_de_los_que_pasaron_los_dos_filtros(self):
        candidatos = [
            offer("Leche Entera La Serenisima 1 Lt", 200_000),
            offer("Leche Entera La Serenisima 1 Lt Larga Vida", 180_000),
            offer("Leche Entera La Serenisima 1 Lt Oferta", 190_000),
            offer("Leche Chocolatada Cindor 200 Ml", 50_000),
        ]
        seleccion = select_candidate("leche entera la serenisima 1l", candidatos)

        assert seleccion.chosen is not None
        assert seleccion.chosen.product.name == "Leche Entera La Serenisima 1 Lt Larga Vida"

    def test_el_barato_equivocado_no_gana_por_barato(self):
        """La chocolatada a $500 es el resultado más barato y el más equivocado:
        la saca el filtro de nombre antes de que llegue a competir."""
        candidatos = [
            offer("Leche Entera La Serenisima 1 Lt", 200_000),
            offer("Leche Chocolatada Cindor 200 Ml", 50_000),
        ]
        seleccion = select_candidate("leche entera la serenisima 1l", candidatos)

        assert seleccion.chosen is not None
        assert seleccion.chosen.product.name == "Leche Entera La Serenisima 1 Lt"
        assert [s.name for s in seleccion.rejected_by_name] == [
            "Leche Chocolatada Cindor 200 Ml"
        ]

    def test_el_sachet_barato_lo_saca_el_filtro_de_precio(self):
        """Pasa el nombre —es la misma marca y producto— pero su precio delata
        que no es la misma presentación."""
        candidatos = [
            offer("Leche Entera La Serenisima 1 Lt", 200_000),
            offer("Leche Entera La Serenisima 1 Lt Pack", 210_000),
            offer("Leche Entera La Serenisima 1 Lt Sachet", 60_000),
        ]
        seleccion = select_candidate("leche entera la serenisima 1 lt", candidatos)

        assert seleccion.chosen is not None
        assert seleccion.chosen.product.name == "Leche Entera La Serenisima 1 Lt"
        assert [p.name for p in seleccion.flagged_prices] == [
            "Leche Entera La Serenisima 1 Lt Sachet"
        ]

    def test_sin_candidatos_aceptables_no_elige_nada(self):
        seleccion = select_candidate("yerba playadito 1kg", [offer("Yerba Mate Union 1 Kg")])

        assert seleccion.chosen is None

    def test_el_unico_sobreviviente_se_juzga_contra_todos_los_candidatos(self, caplog):
        """Cuando el nombre deja uno solo, la mediana de todo lo que devolvió la
        cadena es la única referencia disponible para poder avisar."""
        candidatos = [
            offer("Yerba Mate Playadito Con Palo 1 Kg", 500_000),
            offer("Yerba Mate Union 1 Kg", 100_000),
            offer("Yerba Mate Rosamonte 1 Kg", 110_000),
            offer("Yerba Mate Taragui 1 Kg", 105_000),
        ]
        with caplog.at_level(logging.WARNING, logger="app.services.matching.outliers"):
            seleccion = select_candidate("yerba playadito 1kg", candidatos)

        assert seleccion.chosen is not None
        assert seleccion.chosen.product.name == "Yerba Mate Playadito Con Palo 1 Kg"
        assert "se conserva" in caplog.text
