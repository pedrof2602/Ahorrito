"""Rarezas de serialización de VTEX.

Cada test corresponde a algo observado en las APIs reales, no a un caso teórico.
"""

from __future__ import annotations

import pytest

from app.services.providers.vtex.quirks import (
    normalize_ean,
    parse_resources,
    to_cents,
    unmangle,
)


class TestParseResources:
    def test_acepta_el_formato_real_sin_prefijo(self):
        """Ambas tiendas devuelven `0-2/1567`, no `products 0-2/1567`."""
        assert parse_resources("0-2/1567") == (0, 2, 1567)

    def test_acepta_el_formato_documentado_con_prefijo(self):
        assert parse_resources("products 0-49/1234") == (0, 49, 1234)

    def test_header_ausente_no_es_error(self):
        assert parse_resources(None) is None
        assert parse_resources("") is None

    def test_header_ilegible_devuelve_none(self):
        assert parse_resources("no-tiene-numeros") is None


class TestUnmangle:
    def test_desenmascara_backing_fields_de_carrefour(self):
        """Carrefour serializa Teasers con los backing fields de C# al aire."""
        raw = {"<Name>k__BackingField": "Tarjeta Carrefour 15%"}
        assert unmangle(raw) == {"Name": "Tarjeta Carrefour 15%"}

    def test_desenmascara_en_profundidad(self):
        raw = {
            "<Name>k__BackingField": "Promo",
            "<Effects>k__BackingField": {
                "<Parameters>k__BackingField": [
                    {"<Name>k__BackingField": "PercentualDiscount",
                     "<Value>k__BackingField": "15"}
                ]
            },
        }
        assert unmangle(raw) == {
            "Name": "Promo",
            "Effects": {"Parameters": [{"Name": "PercentualDiscount", "Value": "15"}]},
        }

    def test_deja_intactas_las_claves_normales(self):
        raw = {"Name": "Promo", "Conditions": {"MinimumQuantity": 0}}
        assert unmangle(raw) == raw


class TestNormalizeEan:
    def test_ean13_valido_pasa(self):
        assert normalize_ean("7791720023969") == "7791720023969"

    def test_rellena_ean8_a_13(self):
        # 96385074 es un EAN-8 válido conocido.
        assert normalize_ean("96385074") == "0000096385074"

    def test_rechaza_digito_verificador_invalido(self):
        """Un EAN mal normalizado fusionaría productos distintos y mostraría
        el precio equivocado: ante la duda, no matchear."""
        assert normalize_ean("7791720023968") is None

    def test_rechaza_codigos_internos_de_balanza(self):
        # Prefijo 2 = peso variable: el mismo producto tiene un EAN por paquete.
        assert normalize_ean("2001234000005") is None

    def test_rechaza_vacio_y_ceros(self):
        assert normalize_ean(None) is None
        assert normalize_ean("") is None
        assert normalize_ean("0000000000000") is None

    def test_limpia_separadores(self):
        assert normalize_ean(" 7791720023969 ") == "7791720023969"


class TestToCents:
    @pytest.mark.parametrize(
        ("pesos", "centavos"),
        [(10000.9, 1000090), (3050.0, 305000), (0.1, 10), (None, None)],
    )
    def test_convierte_sin_drift_de_float(self, pesos, centavos):
        """10000.9 * 100 en float da 1000089.9999; hay que redondear."""
        assert to_cents(pesos) == centavos

    def test_valor_no_numerico_devuelve_none(self):
        assert to_cents("no-es-un-numero") is None
