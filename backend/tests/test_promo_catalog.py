"""Tests del catálogo curado de promos bancarias.

El archivo que se versiona es un dato editado a mano, así que lo que se prueba
acá no es lógica: es que un YAML mal cargado falle fuerte y temprano en vez de
dejar la app contestando totales sin ese descuento.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.models.payment import CardKind, Channel, PaymentInstrument, PaymentRail
from app.services.promo_catalog import (
    STALE_AFTER_DAYS,
    load_catalog,
)

VALID = """
- id: banco-x-miercoles
  name: "Banco X 25% miércoles"
  issuer_slug: banco-x
  percent: 25
  weekdays: [mie]
  rails: [modo]
  card_kinds: [debit]
  cap_cents: 1500000
  cap_period: monthly
  benefit_type: reintegro
  valid_from: 2026-01-01
  valid_to: 2026-12-31
  verified_on: 2026-08-31
"""


def write(tmp_path, name: str, content: str):
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


@pytest.fixture
def empty_bins(tmp_path):
    return write(tmp_path, "bins.yaml", "[]\n")


class TestCarga:
    def test_carga_una_regla_valida(self, tmp_path, empty_bins):
        catalog = load_catalog(write(tmp_path, "p.yaml", VALID), empty_bins)
        assert len(catalog.rules) == 1
        rule = catalog.rules[0]
        assert rule.weekdays == frozenset({2})
        assert rule.rails == frozenset({PaymentRail.MODO})

    def test_un_archivo_que_no_existe_no_rompe(self, tmp_path):
        catalog = load_catalog(tmp_path / "no-existe.yaml", tmp_path / "tampoco.yaml")
        assert catalog.rules == ()

    def test_los_ids_repetidos_son_error(self, tmp_path, empty_bins):
        with pytest.raises(ValueError, match="repetidos"):
            load_catalog(write(tmp_path, "p.yaml", VALID + VALID), empty_bins)

    def test_un_dia_mal_escrito_es_error(self, tmp_path, empty_bins):
        roto = VALID.replace("[mie]", "[miercolez]")
        with pytest.raises(Exception, match="miercolez"):
            load_catalog(write(tmp_path, "p.yaml", roto), empty_bins)

    def test_vigencia_al_reves_es_error(self, tmp_path, empty_bins):
        roto = VALID.replace("valid_to: 2026-12-31", "valid_to: 2025-01-01")
        with pytest.raises(Exception, match="valid_to"):
            load_catalog(write(tmp_path, "p.yaml", roto), empty_bins)

    def test_un_yaml_que_no_es_lista_es_error(self, tmp_path, empty_bins):
        with pytest.raises(ValueError, match="lista"):
            load_catalog(write(tmp_path, "p.yaml", "clave: valor\n"), empty_bins)


class TestBins:
    def test_los_bin_se_normalizan_y_deduplican(self, tmp_path):
        bins = write(
            tmp_path,
            "bins.yaml",
            """
- issuer_slug: carrefour-banco
  display_name: Carrefour Banco
  bins: ["507858", "507-858", "858110"]
""",
        )
        catalog = load_catalog(write(tmp_path, "p.yaml", "[]"), bins)
        assert catalog.bins_for("carrefour-banco") == ("507858", "858110")

    def test_un_bin_demasiado_corto_es_error(self, tmp_path):
        """Un BIN de 4 dígitos rompe la simulación: devuelve la respuesta sin
        items, que es indistinguible de 'la sucursal no lo vende'."""
        bins = write(
            tmp_path,
            "bins.yaml",
            '- issuer_slug: x\n  display_name: X\n  bins: ["5078"]\n',
        )
        with pytest.raises(Exception, match="BIN inválido"):
            load_catalog(write(tmp_path, "p.yaml", "[]"), bins)

    def test_emisor_desconocido_devuelve_vacio(self, tmp_path, empty_bins):
        catalog = load_catalog(write(tmp_path, "p.yaml", "[]"), empty_bins)
        assert catalog.bins_for("banco-inexistente") == ()


class TestFiltrado:
    @pytest.fixture
    def catalog(self, tmp_path, empty_bins):
        return load_catalog(write(tmp_path, "p.yaml", VALID), empty_bins)

    @pytest.fixture
    def card(self):
        return PaymentInstrument(
            label="Visa Banco X",
            issuer_slug="banco-x",
            kind=CardKind.DEBIT,
            rails=frozenset({PaymentRail.MODO}),
        )

    def test_aplica_el_miercoles(self, catalog, card):
        got = catalog.rules_for(
            card, chain_slug="disco-ar", channel=Channel.ONLINE, on_date=date(2026, 9, 2)
        )
        assert [r.id for r in got] == ["banco-x-miercoles"]

    def test_no_aplica_el_jueves(self, catalog, card):
        got = catalog.rules_for(
            card, chain_slug="disco-ar", channel=Channel.ONLINE, on_date=date(2026, 9, 3)
        )
        assert got == []

    def test_no_aplica_a_otro_emisor(self, catalog, card):
        otro = card.model_copy(update={"issuer_slug": "banco-z"})
        got = catalog.rules_for(
            otro, chain_slug="disco-ar", channel=Channel.ONLINE, on_date=date(2026, 9, 2)
        )
        assert got == []


class TestFrescura:
    def test_detecta_una_regla_sin_verificar_hace_mucho(self, tmp_path, empty_bins):
        catalog = load_catalog(write(tmp_path, "p.yaml", VALID), empty_bins)
        reciente = date(2026, 9, 1)
        viejo = date(2026, 8, 31) + __import__("datetime").timedelta(
            days=STALE_AFTER_DAYS + 1
        )
        assert catalog.stale(reciente) == []
        assert [r.id for r in catalog.stale(viejo)] == ["banco-x-miercoles"]

    def test_detecta_una_regla_vencida(self, tmp_path, empty_bins):
        vencida = VALID.replace("valid_to: 2026-12-31", "valid_to: 2026-02-01")
        catalog = load_catalog(write(tmp_path, "p.yaml", vencida), empty_bins)
        assert [r.id for r in catalog.stale(date(2026, 3, 1))] == ["banco-x-miercoles"]


class TestCatalogoDelRepo:
    """El YAML que se versiona tiene que cargar y ser coherente."""

    def test_el_catalogo_real_carga(self):
        catalog = load_catalog()
        assert catalog.rules, "no hay ninguna regla cargada"
        assert catalog.issuers, "no hay ningún emisor cargado"

    def test_ninguna_regla_del_repo_esta_vencida_al_verificarla(self):
        for rule in load_catalog().rules:
            assert rule.valid_from <= rule.verified_on <= rule.valid_to, (
                f"{rule.id}: se verificó fuera de su propia vigencia"
            )

    def test_los_bin_de_carrefour_son_los_medidos(self):
        """Salen de `RestrictionsBins` del teaser y activan el descuento."""
        assert "507858" in load_catalog().bins_for("carrefour-banco")
