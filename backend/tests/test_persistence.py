"""Persistencia: histórico de precios e ingesta."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.repositories import (
    ChainRepository,
    PriceRepository,
    ProductRepository,
    StoreRepository,
    promo_digest,
)
from app.db.tables import Base
from app.models.catalog import (
    Chain,
    Offer,
    PriceScope,
    Product,
    Promotion,
    PromotionKind,
    Store,
)

T0 = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        yield s
    await engine.dispose()


def _product(sku="sku1", ean="7791720023969", name="Leche entera 1L"):
    return Product(
        chain_slug="carrefour-ar",
        product_id="p1",
        sku_id=sku,
        name=name,
        ean=ean,
        brand="La Serenísima",
    )


def _offer(price_cents=250000, at=T0, promos=(), available=True):
    return Offer(
        price_cents=price_cents,
        reference_price_cents=None,
        available=available,
        available_quantity=10,
        seller_id="1",
        price_scope=PriceScope.NATIONAL,
        captured_at=at,
        promotions=promos,
    )


@pytest_asyncio.fixture
async def chain_id(session):
    repo = ChainRepository(session)
    row = await repo.upsert(
        Chain(slug="carrefour-ar", display_name="Carrefour", supports_store_prices=True)
    )
    return row.id


@pytest.mark.asyncio
class TestHistoricoSoloAnteCambio:
    """El histórico es una tabla de cambios de precio, no un volcado por corrida.

    Un crawl diario de 50k SKU escribiría ~18M filas al año si guardara todo;
    los precios de supermercado cambian un par de veces al mes.
    """

    async def test_el_primer_precio_se_inserta(self, session, chain_id):
        products, prices = ProductRepository(session), PriceRepository(session)
        sku = await products.upsert_sku(chain_id, _product(), T0)
        assert await prices.record(sku.id, None, _offer()) is True

    async def test_un_precio_sin_cambios_no_crea_fila_nueva(self, session, chain_id):
        products, prices = ProductRepository(session), PriceRepository(session)
        sku = await products.upsert_sku(chain_id, _product(), T0)

        await prices.record(sku.id, None, _offer(at=T0))
        inserted = await prices.record(sku.id, None, _offer(at=T0 + timedelta(days=1)))

        assert inserted is False
        history = await prices.history(sku.id, T0 - timedelta(days=1))
        assert len(history) == 1

    async def test_un_precio_sin_cambios_corre_last_seen_at(self, session, chain_id):
        """Cada fila significa 'esto valió desde observed_at hasta last_seen_at'."""
        products, prices = ProductRepository(session), PriceRepository(session)
        sku = await products.upsert_sku(chain_id, _product(), T0)
        mañana = T0 + timedelta(days=1)

        await prices.record(sku.id, None, _offer(at=T0))
        await prices.record(sku.id, None, _offer(at=mañana))

        snap = await prices.latest(sku.id, None)
        assert snap.observed_at.replace(tzinfo=UTC) == T0
        assert snap.last_seen_at.replace(tzinfo=UTC) == mañana

    async def test_un_cambio_de_precio_crea_fila_nueva(self, session, chain_id):
        products, prices = ProductRepository(session), PriceRepository(session)
        sku = await products.upsert_sku(chain_id, _product(), T0)

        await prices.record(sku.id, None, _offer(250000, at=T0))
        inserted = await prices.record(
            sku.id, None, _offer(280000, at=T0 + timedelta(days=1))
        )

        assert inserted is True
        assert len(await prices.history(sku.id, T0 - timedelta(days=1))) == 2

    async def test_un_cambio_de_disponibilidad_crea_fila_nueva(self, session, chain_id):
        """Que un producto se agote es información, aunque el precio no cambie."""
        products, prices = ProductRepository(session), PriceRepository(session)
        sku = await products.upsert_sku(chain_id, _product(), T0)

        await prices.record(sku.id, None, _offer(at=T0))
        inserted = await prices.record(
            sku.id, None, _offer(at=T0 + timedelta(days=1), available=False)
        )
        assert inserted is True

    async def test_un_cambio_de_promo_crea_fila_nueva(self, session, chain_id):
        """El precio de lista puede no moverse y aun así cambiar lo que pagás."""
        products, prices = ProductRepository(session), PriceRepository(session)
        sku = await products.upsert_sku(chain_id, _product(), T0)
        promo = (Promotion(name="2x1", kind=PromotionKind.BUY_X_GET_Y),)

        await prices.record(sku.id, None, _offer(at=T0))
        inserted = await prices.record(
            sku.id, None, _offer(at=T0 + timedelta(days=1), promos=promo)
        )
        assert inserted is True


class TestPromoDigest:
    def test_el_orden_de_las_promos_no_altera_la_huella(self):
        """VTEX no garantiza el orden; sin ordenar habría falsos cambios."""
        a = Promotion(name="A", kind=PromotionKind.PERCENT_DISCOUNT, percent=10)
        b = Promotion(name="B", kind=PromotionKind.PAYMENT_METHOD, percent=15)
        assert promo_digest((a, b)) == promo_digest((b, a))

    def test_sin_promos_la_huella_es_vacia(self):
        assert promo_digest(()) == ""

    def test_promos_distintas_dan_huellas_distintas(self):
        a = Promotion(name="A", kind=PromotionKind.PERCENT_DISCOUNT, percent=10)
        b = Promotion(name="A", kind=PromotionKind.PERCENT_DISCOUNT, percent=20)
        assert promo_digest((a,)) != promo_digest((b,))


@pytest.mark.asyncio
class TestSkuYProductoCanonico:
    async def test_upsert_de_sku_nuevo_no_viola_not_null(self, session, chain_id):
        """Regresión: resolver el EAN dispara un select, y el autoflush
        intentaba grabar la fila a medio poblar."""
        products = ProductRepository(session)
        row = await products.upsert_sku(chain_id, _product(), T0)
        assert row.external_product_id == "p1"
        assert row.name == "Leche entera 1L"

    async def test_el_mismo_ean_en_dos_cadenas_comparte_producto(self, session):
        """Es lo que hace posible comparar precios entre supermercados."""
        chains, products = ChainRepository(session), ProductRepository(session)
        car = await chains.upsert(Chain(slug="carrefour-ar", display_name="C", supports_store_prices=True))
        disco = await chains.upsert(Chain(slug="disco-ar", display_name="D", supports_store_prices=False))

        a = await products.upsert_sku(car.id, _product(sku="c1"), T0)
        b = await products.upsert_sku(disco.id, _product(sku="d1"), T0)

        assert a.product_id is not None
        assert a.product_id == b.product_id

    async def test_un_sku_sin_ean_queda_sin_vincular(self, session, chain_id):
        """Fusionar por nombre sin umbral mostraría el precio de otro producto."""
        products = ProductRepository(session)
        row = await products.upsert_sku(chain_id, _product(ean=None), T0)
        assert row.product_id is None

    async def test_reprocesar_el_mismo_sku_no_duplica(self, session, chain_id):
        products = ProductRepository(session)
        a = await products.upsert_sku(chain_id, _product(), T0)
        b = await products.upsert_sku(chain_id, _product(name="Nombre nuevo"), T0)
        assert a.id == b.id
        assert b.name == "Nombre nuevo"


@pytest.mark.asyncio
class TestSucursales:
    async def test_los_precios_por_sucursal_no_se_mezclan(self, session, chain_id):
        """Dos sucursales pueden tener precios distintos del mismo SKU; si el
        histórico no los separa, el gráfico oscilaría sin sentido."""
        products, prices = ProductRepository(session), PriceRepository(session)
        stores = StoreRepository(session)
        sku = await products.upsert_sku(chain_id, _product(), T0)

        a = await stores.upsert(chain_id, Store(
            chain_slug="carrefour-ar", external_id="s1", name="Warnes", sales_channel=1))
        b = await stores.upsert(chain_id, Store(
            chain_slug="carrefour-ar", external_id="s2", name="Córdoba", sales_channel=3))

        await prices.record(sku.id, a.id, _offer(250000))
        await prices.record(sku.id, b.id, _offer(300000))

        assert (await prices.latest(sku.id, a.id)).price_cents == 250000
        assert (await prices.latest(sku.id, b.id)).price_cents == 300000

    async def test_reprocesar_una_sucursal_no_duplica(self, session, chain_id):
        stores = StoreRepository(session)
        store = Store(chain_slug="carrefour-ar", external_id="s1", name="Warnes")
        a = await stores.upsert(chain_id, store)
        b = await stores.upsert(chain_id, store)
        assert a.id == b.id
