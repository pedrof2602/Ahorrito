"""La gramática de los carteles de góndola.

Los casos de `TestSemanticaMedida` no son ejemplos inventados: cada uno se
verificó simulando la compra contra el checkout real (ver el docstring de
`promo_text`). Si alguno cambia, cambió la interpretación de una promo y con
ella el ahorro que le mostramos al usuario.
"""

from __future__ import annotations

import pytest

from app.models.catalog import PromotionKind
from app.services.providers.promo_text import membership_required, parse_promo_text


class TestSemanticaMedida:
    """Ahorro sobre el combo, contrastado contra el checkout el 2026-09-01."""

    @pytest.mark.parametrize(
        ("titulo", "esperado"),
        [
            ("2x1", 50.0),
            ("3x2", 33.3),
            ("6x4", 33.3),
            ("2do al 50%", 25.0),
            ("2do al 70%", 35.0),
            ("2do al 80%", 40.0),
            ("70% 2da", 35.0),
        ],
    )
    def test_el_ahorro_efectivo_es_el_medido(self, titulo, esperado):
        mecanica = parse_promo_text(titulo)
        assert mecanica is not None
        assert mecanica.effective_percent == pytest.approx(esperado, abs=0.1)

    def test_el_porcentaje_del_cartel_no_es_el_ahorro(self):
        """El caso que justifica todo el módulo.

        "2do al 70%" dice 70 y ahorra 35. Publicar el 70 como descuento —que es
        lo que hacía el mapper de VTEX leyendo el nombre— duplica el ahorro y
        manda a comprar donde no conviene.
        """
        mecanica = parse_promo_text("PROMO-2do al 70% Max 8 unidades Combinable")
        assert mecanica is not None
        assert mecanica.nth_unit_percent_off == 70
        assert mecanica.effective_percent == pytest.approx(35.0)


class TestExplicacion:
    def test_el_2x1_dice_que_la_segunda_es_gratis(self):
        texto = parse_promo_text("2x1").describe()
        assert texto == (
            "Llevando 2 pagás 1: la 2da unidad te la llevás gratis "
            "(50% de ahorro llevando 2)."
        )

    def test_el_3x2_nombra_la_tercera(self):
        assert "la 3ra unidad te la llevás gratis" in parse_promo_text("3x2").describe()

    def test_el_6x4_cuenta_las_unidades_gratis(self):
        """Con más de una gratis no hay ordinal que sirva: se cuentan."""
        assert "2 unidades gratis" in parse_promo_text("6x4").describe()

    def test_el_2do_al_70_explica_que_el_descuento_es_sobre_una_unidad(self):
        texto = parse_promo_text("2do al 70%").describe()
        assert "la 2da unidad tiene 70% de descuento" in texto
        assert "35% de ahorro llevando 2" in texto

    def test_el_precio_fijo_se_expresa_en_pesos(self):
        texto = parse_promo_text("2x$2500", unit_price_cents=170000).describe()
        assert "pagás $2.500 por las 2 juntas" in texto
        # 3400 de lista contra 2500: 26,5%.
        assert "26,5% de ahorro" in texto

    def test_sin_precio_unitario_no_inventa_el_ahorro(self):
        """Sin saber cuánto vale una unidad, "2x$2500" no dice cuánto ahorrás."""
        mecanica = parse_promo_text("2x$2500")
        assert mecanica is not None
        assert mecanica.bundle_price_cents == 250000
        assert mecanica.effective_percent is None
        assert "de ahorro" not in mecanica.describe()


class TestFormasRealesDelCatalogo:
    """Títulos tal como los publican las cadenas, con su ruido."""

    @pytest.mark.parametrize(
        "titulo",
        [
            "PROMO-3x2 Max 48 unidades Combinable 7 UP-Reg-3-100-Gigante1 al 8.9",
            "PROMO-2do al 50% Iguales-Reg-2-50-OFBebidas1 al 8.9",
            "PROMO-Exclusivo online 2do al 70% Iguales-Reg-2-70-Cocacola21/7 al 31/9",
            "PROMO-2do al 80% Mi Crf Max 12 unidades Combinable NESTLE-Reg-2-80",
            "50% 2da",
            "2x$1100",
        ],
    )
    def test_los_reconoce_entre_el_ruido(self, titulo):
        mecanica = parse_promo_text(titulo, unit_price_cents=100000)
        assert mecanica is not None
        assert mecanica.kind is PromotionKind.BUY_X_GET_Y
        assert mecanica.describe()

    @pytest.mark.parametrize(
        "titulo",
        [
            "Tarjeta Carrefour 15%",
            "Tarjeta Carrefour 20% Off Martes",
            "40% Off Tarjeta Carrefour o Cuenta digital Max 8  1 al 8.9",
            "35%Dto",
            "+10%",
            "Precio con tarjeta: $2249.25",
            "",
        ],
    )
    def test_no_inventa_mecanica_donde_no_la_hay(self, titulo):
        """Un descuento directo o de tarjeta no tiene "llevando N": son promos
        de otro tipo y el `percent` de siempre ya las describe."""
        assert parse_promo_text(titulo) is None

    @pytest.mark.parametrize("titulo", ["1x1", "2x3", "Reg-2-50"])
    def test_descarta_las_formas_que_no_son_promo(self, titulo):
        assert parse_promo_text(titulo) is None


class TestPertenencia:
    def test_detecta_la_comunidad_de_carrefour(self):
        assert membership_required(
            "PROMO-2do al 50% Mi Crf Max 48 unidades Combinable CORONA"
        ) == ("Mi Carrefour",)

    def test_devuelve_las_dos_cuando_la_promo_acepta_cualquiera(self):
        """"Tarjeta Carrefour o Cuenta digital": quedarse con una sola le diría
        al usuario que no le alcanza cuando sí."""
        assert membership_required(
            "40% Off Tarjeta Carrefour o Cuenta digital Max 8  1 al 8.9"
        ) == ("Cuenta Digital Carrefour", "Tarjeta Carrefour")

    def test_la_promo_sin_marca_no_afirma_nada(self):
        assert membership_required("2x1") == ()
