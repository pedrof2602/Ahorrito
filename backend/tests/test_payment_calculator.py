"""Tests del calculador de medios de pago.

Todos los casos de acá son formas concretas de producir un número creíble y
equivocado. Si alguno se rompe, la app le está diciendo al usuario que compre
donde no conviene.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.models.catalog import BasketSimulation, Promotion, PromotionKind
from app.models.payment import (
    BenefitType,
    CapPeriod,
    CardKind,
    Channel,
    PaymentConfidence,
    PaymentInstrument,
    PaymentPromoRule,
    PaymentRail,
)
from app.services.payment import evaluate_option, rank_options

# ---------------------------------------------------------------- constructores


def instrument(**overrides) -> PaymentInstrument:
    base = dict(
        label="Tarjeta Carrefour",
        issuer_slug="carrefour-banco",
        brand="mastercard",
        kind=CardKind.CREDIT,
        rails=frozenset({PaymentRail.CARD}),
        bins=("507858",),
    )
    return PaymentInstrument(**{**base, **overrides})


def rule(**overrides) -> PaymentPromoRule:
    base = dict(
        id="banco-x",
        name="Banco X 25%",
        issuer_slug="banco-nacion",
        percent=25,
        valid_from=date(2026, 1, 1),
        valid_to=date(2026, 12, 31),
        verified_on=date(2026, 8, 31),
    )
    return PaymentPromoRule(**{**base, **overrides})


def simulation(total: int, *, items: int | None = None, **overrides) -> BasketSimulation:
    items_total = items if items is not None else total
    base = dict(
        items_total_cents=items_total,
        discount_cents=items_total - total,
        total_cents=total,
        confirmed_sku_ids=frozenset({"1"}),
    )
    return BasketSimulation(**{**base, **overrides})


def evaluate(**overrides):
    base = dict(
        chain_slug="carrefour-ar",
        chain_display_name="Carrefour",
        instrument=instrument(),
        base_total_cents=100_000,
    )
    return evaluate_option(**{**base, **overrides})


# ------------------------------------------------------- el ahorro se mide, no se calcula


class TestAhorroDelSuper:
    def test_el_ahorro_de_la_tarjeta_es_la_resta_de_dos_simulaciones(self):
        """No es `total x porcentaje`: las promos de VTEX no se acumulan.

        Medido: sobre una canasta real el ahorro de "Tarjeta Carrefour 15%" fue
        $1.499 y la cuenta ingenua daba $3.991, porque en un ítem ya corría un
        30% mejor y en otro la tarjeta desplazó a la promo vigente.
        """
        option = evaluate(
            base_total_cents=2_660_200,
            baseline_simulation=simulation(2_260_870, items=2_660_200),
            simulation=simulation(2_110_960, items=2_660_200),
        )
        assert option.provider_saving_cents == 149_910
        assert option.final_total_cents == 2_110_960
        # El 15% del total sería 399.030: casi tres veces el ahorro real.
        assert option.provider_saving_cents < 2_660_200 * 0.15

    def test_las_promos_por_cantidad_no_se_le_atribuyen_a_la_tarjeta(self):
        """El checkout descuenta cosas que el catálogo no muestra.

        Restarle el total con tarjeta al total de catálogo le regalaría a la
        tarjeta un ahorro que ya existía sin ella.
        """
        option = evaluate(
            base_total_cents=100_000,
            baseline_simulation=simulation(90_000, items=100_000),  # 2do al 50%
            simulation=simulation(85_000, items=100_000),
        )
        assert option.reference_total_cents == 90_000
        assert option.provider_saving_cents == 5_000

    def test_una_simulacion_incompleta_no_se_usa(self):
        """Un total al que le falta un SKU es más barato por estar incompleto."""
        option = evaluate(
            simulation=simulation(
                50_000, confirmed_sku_ids=frozenset({"1"}), missing_sku_ids=frozenset({"2"})
            ),
        )
        assert option.final_total_cents == 100_000
        assert option.provider_saving_cents == 0
        assert any("no confirmó" in c for c in option.caveats)

    def test_sin_checkout_consultable_cae_al_catalogo_y_lo_declara(self):
        """El caso Disco: no hay nada medido y hay que decirlo."""
        option = evaluate(chain_slug="disco-ar", chain_display_name="Disco")
        assert option.final_total_cents == 100_000
        assert option.confidence is PaymentConfidence.ESTIMATED
        assert any("catálogo" in c for c in option.caveats)


# ------------------------------------------------------------------------ topes


class TestTopes:
    def test_el_tope_acota_el_ahorro_no_la_compra(self):
        """El error clásico es aplicar el % hasta que la compra llegue al tope.

        Con 25% y tope $15.000 sobre una compra de $100.000: el ahorro es
        $15.000, no el 25% de $15.000 ni el 25% hasta agotar.
        """
        option = evaluate(
            base_total_cents=10_000_000,
            rules=[rule(cap_cents=1_500_000)],
            instrument=instrument(issuer_slug="banco-nacion"),
        )
        assert option.bank_promo is not None
        assert option.bank_promo.uncapped_saving_cents == 2_500_000
        assert option.bank_promo.saving_cents == 1_500_000
        assert option.bank_promo.capped is True

    def test_el_consumo_previo_del_tope_se_descuenta(self):
        """$12.000 usados de un tope de $15.000 dejan $3.000, no $15.000."""
        option = evaluate(
            base_total_cents=10_000_000,
            rules=[rule(cap_cents=1_500_000, cap_period=CapPeriod.MONTHLY)],
            instrument=instrument(issuer_slug="banco-nacion"),
            cap_used_cents={"banco-x": 1_200_000},
        )
        assert option.bank_promo is not None
        assert option.bank_promo.saving_cents == 300_000
        assert option.bank_promo.cap_remaining_cents == 300_000
        assert option.bank_promo.capped is True

    def test_un_tope_agotado_no_es_un_beneficio(self):
        option = evaluate(
            rules=[rule(cap_cents=1_500_000)],
            instrument=instrument(issuer_slug="banco-nacion"),
            cap_used_cents={"banco-x": 1_500_000},
        )
        assert option.bank_promo is None
        assert option.final_total_cents == 100_000

    def test_sin_tope_el_porcentaje_va_entero(self):
        option = evaluate(
            base_total_cents=1_000_000,
            rules=[rule(cap_cents=None)],
            instrument=instrument(issuer_slug="banco-nacion"),
        )
        assert option.bank_promo is not None
        assert option.bank_promo.saving_cents == 250_000
        assert option.bank_promo.capped is False

    def test_avisa_cuando_el_tope_mordio(self):
        option = evaluate(
            base_total_cents=10_000_000,
            rules=[rule(cap_cents=1_500_000, cap_period=CapPeriod.MONTHLY)],
            instrument=instrument(issuer_slug="banco-nacion"),
        )
        assert any("tope mensual" in c for c in option.caveats)


# ------------------------------------------------------------------ acumulación


class TestAcumulacion:
    def test_la_bancaria_no_se_suma_a_la_del_super_por_defecto(self):
        """Los supers publican que sus promos no acumulan con reintegros."""
        option = evaluate(
            baseline_simulation=simulation(100_000),
            simulation=simulation(85_000, items=100_000),
            rules=[rule(stacks_with_provider_promos=False)],
            instrument=instrument(issuer_slug="banco-nacion"),
        )
        assert option.provider_saving_cents == 15_000
        assert option.bank_promo is None
        assert option.final_total_cents == 85_000

    def test_se_suma_solo_si_la_regla_lo_declara(self):
        option = evaluate(
            baseline_simulation=simulation(100_000),
            simulation=simulation(85_000, items=100_000),
            rules=[rule(stacks_with_provider_promos=True, cap_cents=None)],
            instrument=instrument(issuer_slug="banco-nacion"),
        )
        assert option.bank_promo is not None
        # El banco descuenta sobre lo que pagás, no sobre el precio de lista.
        assert option.bank_promo.saving_cents == 21_250
        assert option.final_total_cents == 63_750
        assert option.confidence is PaymentConfidence.MIXED

    def test_sin_promo_del_super_la_bancaria_aplica_igual(self):
        """Es el caso de Disco: no hay nada que acumular, así que no hay conflicto."""
        option = evaluate(
            chain_slug="disco-ar",
            rules=[rule(stacks_with_provider_promos=False, cap_cents=None)],
            instrument=instrument(issuer_slug="banco-nacion"),
        )
        assert option.bank_promo is not None
        assert option.final_total_cents == 75_000

    def test_entre_varias_reglas_gana_la_que_mas_ahorra(self):
        option = evaluate(
            base_total_cents=1_000_000,
            rules=[
                rule(id="chica", percent=10, cap_cents=None),
                rule(id="grande", percent=30, cap_cents=None),
                rule(id="topeada", percent=50, cap_cents=50_000),
            ],
            instrument=instrument(issuer_slug="banco-nacion"),
        )
        assert option.bank_promo is not None
        assert option.bank_promo.rule_id == "grande"
        assert option.bank_promo.saving_cents == 300_000


# ------------------------------------------------------------------- reintegros


class TestReintegros:
    def test_el_reintegro_baja_el_total_pero_no_el_desembolso(self):
        option = evaluate(
            base_total_cents=1_000_000,
            rules=[rule(benefit_type=BenefitType.REINTEGRO, cap_cents=None)],
            instrument=instrument(issuer_slug="banco-nacion"),
        )
        assert option.final_total_cents == 750_000
        assert option.out_of_pocket_today_cents == 1_000_000
        assert option.is_reimbursement is True
        assert any("reintegro" in c for c in option.caveats)

    def test_el_descuento_en_el_acto_baja_los_dos(self):
        option = evaluate(
            base_total_cents=1_000_000,
            rules=[rule(benefit_type=BenefitType.DISCOUNT, cap_cents=None)],
            instrument=instrument(issuer_slug="banco-nacion"),
        )
        assert option.final_total_cents == 750_000
        assert option.out_of_pocket_today_cents == 750_000
        assert option.is_reimbursement is False


# ------------------------------------------------------------------ elegibilidad


class TestElegibilidad:
    def test_un_miercoles_no_aplica_un_martes(self):
        miercoles = rule(weekdays=["mie"])
        assert miercoles.is_valid_on(date(2026, 9, 2)) is True
        assert miercoles.is_valid_on(date(2026, 9, 1)) is False

    def test_una_regla_vencida_no_aplica(self):
        vencida = rule(valid_to=date(2026, 6, 30))
        assert vencida.is_valid_on(date(2026, 9, 2)) is False

    def test_una_promo_presencial_no_aplica_online(self):
        presencial = rule(channels=[Channel.IN_STORE])
        card = instrument(issuer_slug="banco-nacion")
        assert presencial.matches(card, chain_slug="disco-ar", channel=Channel.ONLINE) is False
        assert presencial.matches(card, chain_slug="disco-ar", channel=Channel.IN_STORE) is True

    def test_el_riel_es_parte_de_la_promo(self):
        """La misma Visa puede tener 25% por MODO y nada por posnet."""
        solo_modo = rule(rails=[PaymentRail.MODO])
        por_posnet = instrument(issuer_slug="banco-nacion", rails=frozenset({PaymentRail.CARD}))
        por_modo = instrument(issuer_slug="banco-nacion", rails=frozenset({PaymentRail.MODO}))
        assert solo_modo.matches(por_posnet, chain_slug="disco-ar", channel=Channel.ONLINE) is False
        assert solo_modo.matches(por_modo, chain_slug="disco-ar", channel=Channel.ONLINE) is True

    def test_la_regla_no_alcanza_a_otra_cadena(self):
        solo_disco = rule(chain_slugs={"disco-ar"})
        card = instrument(issuer_slug="banco-nacion")
        assert solo_disco.matches(card, chain_slug="carrefour-ar", channel=Channel.ONLINE) is False

    def test_el_minimo_de_compra_se_respeta(self):
        option = evaluate(
            base_total_cents=50_000,
            rules=[rule(minimum_purchase_cents=100_000, cap_cents=None)],
            instrument=instrument(issuer_slug="banco-nacion"),
        )
        assert option.bank_promo is None

    def test_dinero_en_cuenta_nunca_va_a_la_simulacion(self):
        """Cuenta DNI y saldo de MP no pasan por la red de tarjetas: no tienen BIN."""
        saldo = instrument(
            label="Cuenta DNI",
            issuer_slug="banco-provincia",
            kind=CardKind.ACCOUNT_BALANCE,
            rails=frozenset({PaymentRail.CUENTA_DNI}),
            bins=(),
        )
        assert saldo.simulatable is False
        option = evaluate(instrument=saldo)
        assert any("Sin BIN" in c for c in option.caveats)


# --------------------------------------------------------------------- confianza


class TestConfianza:
    def test_solo_simulacion_es_medido(self):
        option = evaluate(
            baseline_simulation=simulation(100_000),
            simulation=simulation(85_000, items=100_000),
        )
        assert option.confidence is PaymentConfidence.MEASURED

    def test_simulacion_mas_regla_curada_es_mixto(self):
        option = evaluate(
            baseline_simulation=simulation(100_000),
            simulation=simulation(85_000, items=100_000),
            rules=[rule(stacks_with_provider_promos=True, cap_cents=None)],
            instrument=instrument(issuer_slug="banco-nacion"),
        )
        assert option.confidence is PaymentConfidence.MIXED

    def test_solo_regla_curada_es_estimado(self):
        option = evaluate(
            rules=[rule(cap_cents=None)],
            instrument=instrument(issuer_slug="banco-nacion"),
        )
        assert option.confidence is PaymentConfidence.ESTIMATED

    def test_una_regla_sin_verificar_se_avisa(self):
        option = evaluate(
            rules=[rule(cap_cents=None, unverified=True)],
            instrument=instrument(issuer_slug="banco-nacion"),
        )
        assert option.bank_promo is not None
        assert option.bank_promo.unverified is True
        assert any("no está verificada" in c for c in option.caveats)


# ---------------------------------------------------------------------- ranking


class TestRanking:
    def test_ordena_por_lo_que_terminas_pagando(self):
        barato = evaluate(chain_slug="disco-ar", base_total_cents=90_000)
        caro = evaluate(chain_slug="carrefour-ar", base_total_cents=100_000)
        assert [o.chain_slug for o in rank_options([caro, barato])] == [
            "disco-ar",
            "carrefour-ar",
        ]

    def test_la_cadena_a_la_que_le_faltan_productos_no_encabeza(self):
        """Disco sale $100 más barato pero no resuelve dos líneas de la lista.

        Esos dos productos se pagan igual en otro lado, así que su total no es
        el precio de la compra y mandar ahí sería mandar a una compra que no
        cierra.
        """
        barato = evaluate(chain_slug="disco-ar", base_total_cents=90_000)
        caro = evaluate(chain_slug="carrefour-ar", base_total_cents=100_000)

        ranked = rank_options([caro, barato], {"disco-ar": 2, "carrefour-ar": 0})

        assert [o.chain_slug for o in ranked] == ["carrefour-ar", "disco-ar"]

    def test_entre_dos_incompletas_manda_cuantas_le_faltan(self):
        barato = evaluate(chain_slug="disco-ar", base_total_cents=90_000)
        caro = evaluate(chain_slug="carrefour-ar", base_total_cents=100_000)

        ranked = rank_options([caro, barato], {"disco-ar": 3, "carrefour-ar": 1})

        assert [o.chain_slug for o in ranked] == ["carrefour-ar", "disco-ar"]

    def test_sin_faltantes_el_orden_sigue_siendo_el_precio(self):
        """La regla no se activa sola: con las dos cadenas completas manda lo
        que terminás pagando, como siempre."""
        barato = evaluate(chain_slug="disco-ar", base_total_cents=90_000)
        caro = evaluate(chain_slug="carrefour-ar", base_total_cents=100_000)

        ranked = rank_options([caro, barato], {"disco-ar": 0, "carrefour-ar": 0})

        assert [o.chain_slug for o in ranked] == ["disco-ar", "carrefour-ar"]

    def test_a_igual_total_gana_lo_medido(self):
        medido = evaluate(
            baseline_simulation=simulation(100_000),
            simulation=simulation(100_000),
        )
        estimado = evaluate(chain_slug="disco-ar")
        assert medido.final_total_cents == estimado.final_total_cents
        assert rank_options([estimado, medido])[0].confidence is PaymentConfidence.MEASURED

    def test_a_igual_ahorro_gana_el_descuento_sobre_el_reintegro(self):
        en_el_acto = evaluate(
            base_total_cents=1_000_000,
            rules=[rule(id="acto", benefit_type=BenefitType.DISCOUNT, cap_cents=None)],
            instrument=instrument(issuer_slug="banco-nacion"),
        )
        reintegro = evaluate(
            chain_slug="disco-ar",
            base_total_cents=1_000_000,
            rules=[rule(id="reint", benefit_type=BenefitType.REINTEGRO, cap_cents=None)],
            instrument=instrument(issuer_slug="banco-nacion"),
        )
        assert en_el_acto.final_total_cents == reintegro.final_total_cents
        assert rank_options([reintegro, en_el_acto])[0].bank_promo.rule_id == "acto"


@pytest.mark.parametrize("total", [0, 1, 999_999_999])
def test_no_explota_con_totales_extremos(total: int):
    option = evaluate(base_total_cents=total, rules=[rule(cap_cents=None)],
                      instrument=instrument(issuer_slug="banco-nacion"))
    assert option.final_total_cents >= 0
    assert option.out_of_pocket_today_cents >= 0
