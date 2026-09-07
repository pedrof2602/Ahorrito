"""Comparación de una lista de compra completa.

La selección es una función pura, así que casi todo se prueba sin red ni base:
se arman los candidatos a mano en el orden de relevancia que devolvería la cadena.

Cada test cubre una forma concreta de dar un número convincente y equivocado.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.models.basket import BasketLine, MatchConfidence
from app.models.catalog import (
    Chain,
    Offer,
    PriceScope,
    Product,
    ProductOffer,
    Store,
)
from app.services.basket import aggregate, select_line

T0 = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)

CARREFOUR = "carrefour-ar"
DISCO = "disco-ar"
CHAINS = [CARREFOUR, DISCO]


def offer(
    chain: str,
    name: str,
    price_cents: int,
    *,
    ean: str | None = None,
    sku: str | None = None,
    available: bool = True,
) -> ProductOffer:
    return ProductOffer(
        product=Product(
            chain_slug=chain,
            product_id=f"p-{sku or ean or name}",
            sku_id=sku or f"s-{ean or name}",
            name=name,
            ean=ean,
        ),
        offer=Offer(
            price_cents=price_cents,
            available=available,
            captured_at=T0,
            price_scope=PriceScope.NATIONAL,
        ),
    )


class FakeProvider:
    """Doble trivial: `PriceProvider` es un Protocol, no hace falta registrarse."""

    def __init__(self, slug: str, display_name: str) -> None:
        self._chain = Chain(
            slug=slug, display_name=display_name, supports_store_prices=False
        )

    @property
    def chain(self) -> Chain:
        return self._chain


PROVIDERS = [FakeProvider(CARREFOUR, "Carrefour"), FakeProvider(DISCO, "Disco")]


def line(query: str, **kwargs) -> BasketLine:
    return BasketLine(query=query, **kwargs)


class TestSeleccionPorLinea:
    def test_gana_el_ean_comun_aunque_no_sea_el_mas_barato_del_pool(self):
        """La trampa clásica: para 'leche', lo más barato es una chocolatada de
        200 ml. Anclar por EAN común es lo que impide sumarla como si fuera el
        litro de leche."""
        result = select_line(
            line("leche entera 1L"),
            {
                CARREFOUR: [
                    offer(CARREFOUR, "Leche entera 1L", 250000, ean="7791234567890"),
                    offer(CARREFOUR, "Chocolatada 200ml", 45000, ean="7799999999999"),
                ],
                DISCO: [
                    offer(DISCO, "Leche entera 1L", 238000, ean="7791234567890"),
                    offer(DISCO, "Leche en polvo 100g", 30000, ean="7798888888888"),
                ],
            },
            CHAINS,
        )

        assert result.confidence is MatchConfidence.EXACT_EAN
        assert result.ean == "7791234567890"
        assert {m.line_total_cents for m in result.matches} == {250000, 238000}
        assert result.cheapest_chain == DISCO
        assert result.saving_cents == 12000

    def test_sin_ean_comun_cada_cadena_toma_su_mejor_coincidencia(self):
        result = select_line(
            line("pan lactal"),
            {
                CARREFOUR: [offer(CARREFOUR, "Bimbo Artesano 550g", 410000, ean="1")],
                DISCO: [offer(DISCO, "Fargo Blanco 600g", 385000, ean="2")],
            },
            CHAINS,
        )

        assert result.confidence is MatchConfidence.NAME
        assert result.ean is None, "Sin EAN común no hay EAN de línea que declarar"
        assert len(result.matches) == 2
        assert result.comparable is True

    def test_un_ean_fijado_ignora_la_heuristica(self):
        result = select_line(
            line("coca cola", ean="7790895000997"),
            {
                CARREFOUR: [
                    offer(CARREFOUR, "Coca Cola 1.5L", 200000, ean="7790000000000"),
                    offer(CARREFOUR, "Coca Cola 2.25L", 319000, ean="7790895000997"),
                ],
                DISCO: [
                    offer(DISCO, "Coca Cola 1.5L", 190000, ean="7790000000000"),
                    offer(DISCO, "Coca Cola 2.25L", 298000, ean="7790895000997"),
                ],
            },
            CHAINS,
        )

        assert result.confidence is MatchConfidence.PINNED
        assert result.ean == "7790895000997"
        assert {m.line_total_cents for m in result.matches} == {319000, 298000}

    def test_la_cantidad_multiplica_el_precio_de_la_linea(self):
        result = select_line(
            line("leche", quantity=3),
            {
                CARREFOUR: [offer(CARREFOUR, "Leche 1L", 250000, ean="7791")],
                DISCO: [offer(DISCO, "Leche 1L", 238000, ean="7791")],
            },
            CHAINS,
        )
        assert sorted(m.line_total_cents for m in result.matches) == [714000, 750000]
        assert result.saving_cents == 36000

    def test_un_producto_sin_stock_no_entra_en_el_comparable(self):
        """Un precio excelente de algo agotado no es una oferta."""
        result = select_line(
            line("yerba mate 1kg"),
            {
                CARREFOUR: [offer(CARREFOUR, "Yerba 1kg", 500000, ean="7791")],
                DISCO: [
                    offer(DISCO, "Yerba 1kg", 400000, ean="7791", available=False)
                ],
            },
            CHAINS,
        )
        assert len(result.matches) == 2
        assert result.comparable is False

    def test_la_cadena_sin_stock_no_gana_la_linea_por_ser_mas_barata(self):
        """Disco publica la yerba $1.000 más barata pero agotada.

        Encabezar la línea con ella la mostraría como la opción a elegir, y el
        ahorro anunciado sería sobre algo que no te podés llevar.
        """
        result = select_line(
            line("yerba mate 1kg"),
            {
                CARREFOUR: [offer(CARREFOUR, "Yerba 1kg", 500000, ean="7791")],
                DISCO: [offer(DISCO, "Yerba 1kg", 400000, ean="7791", available=False)],
            },
            CHAINS,
        )

        assert result.cheapest_chain == CARREFOUR
        assert result.matches[0].chain_slug == CARREFOUR
        assert result.saving_cents == 0

    def test_sin_stock_en_ninguna_cadena_no_hay_mas_barata(self):
        """El precio de algo que nadie tiene no le gana a nada."""
        result = select_line(
            line("yerba mate 1kg"),
            {
                CARREFOUR: [
                    offer(CARREFOUR, "Yerba 1kg", 500000, ean="7791", available=False)
                ],
                DISCO: [offer(DISCO, "Yerba 1kg", 400000, ean="7791", available=False)],
            },
            CHAINS,
        )

        assert result.cheapest_chain is None
        assert result.saving_cents == 0

    def test_entre_dos_ean_comunes_gana_el_disponible_en_todas(self):
        result = select_line(
            line("arroz 1kg"),
            {
                CARREFOUR: [
                    offer(CARREFOUR, "Arroz Gallo 1kg", 300000, ean="7791"),
                    offer(CARREFOUR, "Arroz Lucchetti 1kg", 320000, ean="7792"),
                ],
                DISCO: [
                    offer(DISCO, "Arroz Gallo 1kg", 290000, ean="7791", available=False),
                    offer(DISCO, "Arroz Lucchetti 1kg", 310000, ean="7792"),
                ],
            },
            CHAINS,
        )
        assert result.ean == "7792", "El mejor rankeado estaba agotado en Disco"
        assert result.comparable is True

    def test_una_cadena_sin_resultados_deja_la_linea_incomparable(self):
        result = select_line(
            line("kombucha"),
            {CARREFOUR: [offer(CARREFOUR, "Kombucha 500ml", 350000, ean="7791")], DISCO: []},
            CHAINS,
        )
        assert result.missing_in == [DISCO]
        assert result.comparable is False
        assert result.confidence is MatchConfidence.NAME

    def test_varias_ofertas_del_mismo_producto_se_colapsan(self):
        """Varios sellers del mismo EAN son el mismo producto: se queda la más
        barata disponible, sin ocupar dos lugares del ranking."""
        result = select_line(
            line("leche"),
            {
                CARREFOUR: [
                    offer(CARREFOUR, "Leche 1L", 260000, ean="7791", sku="a"),
                    offer(CARREFOUR, "Leche 1L", 250000, ean="7791", sku="b"),
                ],
                DISCO: [offer(DISCO, "Leche 1L", 238000, ean="7791")],
            },
            CHAINS,
        )
        carrefour_matches = [m for m in result.matches if m.chain_slug == CARREFOUR]
        assert len(carrefour_matches) == 1
        assert carrefour_matches[0].line_total_cents == 250000

    def test_una_sola_cadena_no_alcanza_para_declarar_coincidencia_exacta(self):
        """Con una cadena sola no hay nada 'común' que verificar; decir
        `exact_ean` sería inventar una garantía."""
        result = select_line(
            line("leche"),
            {CARREFOUR: [offer(CARREFOUR, "Leche 1L", 250000, ean="7791")]},
            [CARREFOUR],
        )
        assert result.confidence is MatchConfidence.NAME
        assert result.comparable is True


class TestTotales:
    def _canasta(self):
        """Disco es más barato en lo que tiene, pero le faltan dos líneas."""
        comun_a = select_line(
            line("leche"),
            {
                CARREFOUR: [offer(CARREFOUR, "Leche 1L", 250000, ean="1")],
                DISCO: [offer(DISCO, "Leche 1L", 238000, ean="1")],
            },
            CHAINS,
        )
        comun_b = select_line(
            line("arroz"),
            {
                CARREFOUR: [offer(CARREFOUR, "Arroz 1kg", 300000, ean="2")],
                DISCO: [offer(DISCO, "Arroz 1kg", 295000, ean="2")],
            },
            CHAINS,
        )
        solo_carrefour_1 = select_line(
            line("yerba"),
            {CARREFOUR: [offer(CARREFOUR, "Yerba 1kg", 500000, ean="3")], DISCO: []},
            CHAINS,
        )
        solo_carrefour_2 = select_line(
            line("pan lactal"),
            {CARREFOUR: [offer(CARREFOUR, "Pan lactal", 410000, ean="4")], DISCO: []},
            CHAINS,
        )
        return [comun_a, comun_b, solo_carrefour_1, solo_carrefour_2]

    def test_una_cadena_incompleta_no_gana_por_el_total_completo(self):
        """El test que justifica el diseño entero.

        Disco suma $5.330 contra $14.600 de Carrefour, pero solo porque le faltan
        la yerba y el pan. Sobre lo que ambas tienen, la diferencia real es $170:
        ese es el número con el que se comparan, y no el total crudo.
        """
        totals = aggregate(self._canasta(), PROVIDERS, {})
        by_slug = {t.chain_slug: t for t in totals}

        assert by_slug[DISCO].total_cents < by_slug[CARREFOUR].total_cents
        assert by_slug[DISCO].comparable_total_cents == 533000
        assert by_slug[CARREFOUR].comparable_total_cents == 550000
        assert by_slug[DISCO].missing_lines == ["yerba", "pan lactal"]
        assert by_slug[CARREFOUR].missing_lines == []

    def test_la_cadena_a_la_que_le_faltan_productos_no_encabeza(self):
        """Disco es más barato en lo comparable ($5.330 contra $5.500) y aun así
        va segundo: no tiene la yerba ni el pan.

        Mandarte a Disco sería mandarte a una compra que no cierra —esos dos
        ítems los vas a pagar en otro lado— y el total que los omite no es el
        precio de tu compra.
        """
        totals = aggregate(self._canasta(), PROVIDERS, {})

        assert [t.chain_slug for t in totals] == [CARREFOUR, DISCO]
        assert totals[0].complete
        assert not totals[1].complete

    def test_entre_dos_incompletas_gana_la_que_resuelve_mas(self):
        """Cuando ninguna tiene todo, el criterio sigue siendo cuánto resuelve
        cada una; el precio desempata recién después."""
        lines = [
            select_line(
                line("leche"),
                {
                    CARREFOUR: [offer(CARREFOUR, "Leche 1L", 900000, ean="1")],
                    DISCO: [offer(DISCO, "Leche 1L", 100000, ean="1")],
                },
                CHAINS,
            ),
            select_line(
                line("yerba"),
                {CARREFOUR: [offer(CARREFOUR, "Yerba 1kg", 500000, ean="3")], DISCO: []},
                CHAINS,
            ),
            select_line(
                line("pan lactal"),
                {CARREFOUR: [offer(CARREFOUR, "Pan", 410000, ean="4")], DISCO: []},
                CHAINS,
            ),
            select_line(
                line("fideos"),
                {CARREFOUR: [], DISCO: [offer(DISCO, "Fideos", 150000, ean="5")]},
                CHAINS,
            ),
        ]
        totals = aggregate(lines, PROVIDERS, {})

        # A Carrefour le falta 1 línea y a Disco 2, aunque Disco sea más barato.
        assert [t.chain_slug for t in totals] == [CARREFOUR, DISCO]
        assert not totals[0].complete

    def test_el_producto_sin_stock_cuenta_como_no_resuelto(self):
        """Tenerlo publicado y no poder llevártelo son lo mismo del lado del
        changuito, así que pesa igual que un faltante en el ranking."""
        lines = [
            select_line(
                line("leche"),
                {
                    CARREFOUR: [offer(CARREFOUR, "Leche 1L", 250000, ean="1")],
                    DISCO: [offer(DISCO, "Leche 1L", 100000, ean="1", available=False)],
                },
                CHAINS,
            ),
            select_line(
                line("arroz"),
                {
                    CARREFOUR: [offer(CARREFOUR, "Arroz 1kg", 300000, ean="2")],
                    DISCO: [offer(DISCO, "Arroz 1kg", 295000, ean="2")],
                },
                CHAINS,
            ),
        ]
        totals = aggregate(lines, PROVIDERS, {})
        by_slug = {t.chain_slug: t for t in totals}

        assert by_slug[DISCO].unavailable_lines == ["leche"]
        assert by_slug[DISCO].missing_lines == []
        assert not by_slug[DISCO].complete
        assert [t.chain_slug for t in totals] == [CARREFOUR, DISCO]

    def test_el_ranking_ordena_por_el_total_comparable(self):
        """Carrefour más barato en lo comparable, pero con un total crudo mayor."""
        lines = [
            select_line(
                line("leche"),
                {
                    CARREFOUR: [offer(CARREFOUR, "Leche 1L", 200000, ean="1")],
                    DISCO: [offer(DISCO, "Leche 1L", 300000, ean="1")],
                },
                CHAINS,
            ),
            select_line(
                line("yerba"),
                {CARREFOUR: [offer(CARREFOUR, "Yerba", 900000, ean="2")], DISCO: []},
                CHAINS,
            ),
        ]
        totals = aggregate(lines, PROVIDERS, {})

        assert totals[0].chain_slug == CARREFOUR
        assert totals[0].total_cents > totals[1].total_cents

    def test_pedir_una_sucursal_no_alcanza_para_que_el_total_sea_de_sucursal(self):
        """Regresión: antes el scope salía de si se había resuelto una sucursal,
        y Carrefour devolvía el mismo total para CP 1425 y 5000 etiquetado como
        'store'. Ahora sale del precio que realmente llegó."""
        store = Store(
            chain_slug=CARREFOUR, external_id="carrefourar0026", name="Hiper Warnes"
        )
        totals = aggregate(self._canasta(), PROVIDERS, {CARREFOUR: store})
        by_slug = {t.chain_slug: t for t in totals}

        assert by_slug[CARREFOUR].price_scope is PriceScope.NATIONAL
        assert by_slug[CARREFOUR].store == store, "La sucursal se informa igual"
        assert by_slug[DISCO].price_scope is PriceScope.NATIONAL

    def test_el_total_vale_lo_que_su_linea_menos_precisa(self):
        """Dos líneas del canal elegido y una nacional dan un total nacional:
        declarar el nivel del mejor componente prometería precisión que el total
        no tiene."""
        def con_scope(chain, price, ean, scope):
            base = offer(chain, f"p{ean}", price, ean=ean)
            return base.model_copy(
                update={"offer": base.offer.model_copy(update={"price_scope": scope})}
            )

        lines = [
            select_line(
                line("leche"),
                {
                    CARREFOUR: [con_scope(CARREFOUR, 200000, "1", PriceScope.CHANNEL)],
                    DISCO: [con_scope(DISCO, 210000, "1", PriceScope.NATIONAL)],
                },
                CHAINS,
            ),
            select_line(
                line("arroz"),
                {
                    CARREFOUR: [con_scope(CARREFOUR, 300000, "2", PriceScope.NATIONAL)],
                    DISCO: [con_scope(DISCO, 310000, "2", PriceScope.NATIONAL)],
                },
                CHAINS,
            ),
        ]
        by_slug = {t.chain_slug: t for t in aggregate(lines, PROVIDERS, {})}
        assert by_slug[CARREFOUR].price_scope is PriceScope.NATIONAL

    def test_un_total_enteramente_de_canal_lo_declara(self):
        def canal(chain, price, ean):
            base = offer(chain, f"p{ean}", price, ean=ean)
            return base.model_copy(
                update={
                    "offer": base.offer.model_copy(
                        update={"price_scope": PriceScope.CHANNEL}
                    )
                }
            )

        lines = [
            select_line(
                line("leche"),
                {CARREFOUR: [canal(CARREFOUR, 200000, "1")], DISCO: [canal(DISCO, 210000, "1")]},
                CHAINS,
            )
        ]
        by_slug = {t.chain_slug: t for t in aggregate(lines, PROVIDERS, {})}
        assert by_slug[CARREFOUR].price_scope is PriceScope.CHANNEL


@pytest.mark.asyncio
class TestOrquestacion:
    async def test_si_una_cadena_falla_entera_no_entra_al_ranking(self):
        """Dejarla adentro vaciaría la canasta comparable y arruinaría el ranking
        de la que sí respondió."""
        from app.services.basket import compare_basket

        registry = _registry(
            {
                CARREFOUR: lambda q: [offer(CARREFOUR, "Leche 1L", 250000, ean="1")],
                DISCO: _boom("Disco no responde"),
            }
        )
        result = await compare_basket(registry, None, _request(["leche"]))

        assert [c.chain_slug for c in result.chains] == [CARREFOUR]
        assert result.partial is True
        assert result.errors[0].chain_slug == DISCO
        assert result.lines[0].comparable is True, (
            "Con una sola cadena en el ranking, su línea sigue siendo su propio total"
        )
        assert result.winner == CARREFOUR

    async def test_un_fallo_en_una_sola_linea_no_saca_a_la_cadena(self):
        from app.services.basket import compare_basket

        def disco(query: str):
            if query == "yerba":
                raise RuntimeError("timeout")
            return [offer(DISCO, "Leche 1L", 238000, ean="1")]

        registry = _registry(
            {
                CARREFOUR: lambda q: [offer(CARREFOUR, f"{q} carrefour", 250000, ean="1")],
                DISCO: disco,
            }
        )
        result = await compare_basket(registry, None, _request(["leche", "yerba"]))

        assert {c.chain_slug for c in result.chains} == {CARREFOUR, DISCO}
        assert result.partial is True
        assert result.comparable_line_count == 1

    async def test_sin_lineas_comunes_no_hay_ganador(self):
        from app.services.basket import compare_basket

        registry = _registry(
            {
                CARREFOUR: lambda q: [offer(CARREFOUR, "Yerba", 500000, ean="3")],
                DISCO: lambda q: [],
            }
        )
        result = await compare_basket(registry, None, _request(["yerba"]))

        assert result.comparable_line_count == 0
        assert result.winner is None

    async def test_un_checkout_incompleto_no_informa_total_confirmado(self):
        """Regresión: Disco rechaza todo SKU en su checkout. Sumar solo lo que
        confirmó daba un total de $0,00 que parecía una ganga."""
        from app.services.basket import compare_basket

        registry = _registry(
            {
                CARREFOUR: lambda q: [offer(CARREFOUR, "Leche 1L", 250000, ean="1")],
                DISCO: lambda q: [offer(DISCO, "Leche 1L", 238000, ean="1")],
            },
            # Carrefour confirma; Disco no devuelve nada (checkout inutilizable).
            verifiers={CARREFOUR: lambda skus: [(s, 250000) for s, _ in skus]},
        )
        request = _request(["leche"])
        request = request.model_copy(update={"verify": True})
        result = await compare_basket(registry, None, request)

        by_slug = {c.chain_slug: c for c in result.chains}
        assert by_slug[CARREFOUR].verified_total_cents == 250000
        assert by_slug[DISCO].verified_total_cents is None, (
            "Sin confirmación completa se informa None, nunca un total parcial"
        )
        assert by_slug[DISCO].total_cents == 238000, "El total de catálogo sigue ahí"

    async def test_un_checkout_que_confirma_de_menos_no_informa_total(self):
        """Confirmar 1 de 2 SKU y sumar solo ese daría el total de media canasta."""
        from app.services.basket import compare_basket

        registry = _registry(
            {CARREFOUR: lambda q: [offer(CARREFOUR, f"{q}", 250000, ean=f"e-{q}")]},
            verifiers={CARREFOUR: lambda skus: [(skus[0][0], 250000)]},
        )
        request = _request(["leche", "yerba"]).model_copy(update={"verify": True})
        result = await compare_basket(registry, None, request)

        assert result.chains[0].verified_total_cents is None
        assert result.chains[0].total_cents == 500000

    async def test_una_consulta_repetida_se_busca_una_sola_vez(self):
        """Dos líneas con el mismo término no deberían pegarle dos veces a la API."""
        from app.services.basket import compare_basket

        calls: list[str] = []

        def counting(query: str):
            calls.append(query)
            return [offer(CARREFOUR, "Leche 1L", 250000, ean="1")]

        registry = _registry({CARREFOUR: counting})
        await compare_basket(registry, None, _request(["leche", "leche"]))

        assert calls == ["leche"]


# --- Andamiaje para los tests de orquestación --------------------------------


def _boom(message: str):
    def fail(query: str):
        raise RuntimeError(message)

    return fail


def _registry(handlers: dict, verifiers: dict | None = None):
    """Registro falso.

    `handlers[slug](query) -> list[ProductOffer]`; `verifiers[slug](sku_qty)`
    devuelve los pares `(sku_id, price_cents)` que el checkout confirma. Una
    cadena sin verificador confirma nada, que es el caso de Disco.
    """
    verifiers = verifiers or {}

    class _Provider(FakeProvider):
        def __init__(self, slug: str, handler) -> None:
            super().__init__(slug, slug.split("-")[0].title())
            self._handler = handler

        async def search(
            self, term: str, *, store=None, sales_channel=None, limit: int = 50
        ):
            return self._handler(term)

        async def verify_prices(self, sku_quantities, *, store=None, sales_channel=None):
            confirm = verifiers.get(self.chain.slug)
            if confirm is None:
                return []
            return [
                offer(self.chain.slug, f"sku {sku}", price, sku=sku)
                for sku, price in confirm(list(sku_quantities))
            ]

    class _Registry:
        def __init__(self, providers) -> None:
            self._providers = providers

        def all(self):
            return list(self._providers)

    return _Registry([_Provider(slug, h) for slug, h in handlers.items()])


def _request(queries: list[str]):
    from app.models.basket import BasketCompareRequest

    return BasketCompareRequest(
        lines=[BasketLine(query=q) for q in queries], persist=False
    )
