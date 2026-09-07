"""Caché de precios: TTL, frescura declarada y respaldo de cadena caída.

Cada test cubre una forma de que la caché mienta: servir un precio vencido como
si fuera fresco, servirlo sin decir que es viejo, servir menos candidatos de los
pedidos, o hacer desaparecer una cadena que sí podíamos mostrar.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.repositories import PriceSearchCacheRepository
from app.db.tables import Base
from app.models.catalog import Chain, Offer, PriceScope, Product, ProductOffer, Store
from app.services.http import ProviderUnavailable
from app.services.ingest import IngestService
from app.services.providers.cache import CachedProvider, bypass_cache, purge_expired

T0 = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)

CARREFOUR = Chain(
    slug="carrefour-ar",
    display_name="Carrefour",
    supports_store_prices=True,
    sales_channels=(1, 3),
    default_sales_channel=1,
)


@pytest_asyncio.fixture
async def factory():
    """Una base en memoria compartida por todas las sesiones del test.

    `StaticPool` + una URL con nombre: sin eso cada conexión de SQLite en memoria
    abre su propia base vacía, y el `CachedProvider` —que abre una sesión corta
    por operación— nunca volvería a ver lo que acaba de guardar.
    """
    engine = create_async_engine(
        "sqlite+aiosqlite:///file:cache_test?mode=memory&cache=shared&uri=true"
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    yield lambda: maker
    await engine.dispose()


def _offer(price_cents: int, *, sku: str = "s1", at: datetime = T0) -> ProductOffer:
    return ProductOffer(
        product=Product(
            chain_slug=CARREFOUR.slug,
            product_id="p1",
            sku_id=sku,
            name="Leche entera 1L",
            ean="7791720023969",
        ),
        offer=Offer(
            price_cents=price_cents,
            available=True,
            captured_at=at,
            price_scope=PriceScope.NATIONAL,
        ),
    )


class FakeProvider:
    """Cadena de mentira que cuenta las llamadas y puede romperse a pedido."""

    def __init__(self, offers: list[ProductOffer] | None = None) -> None:
        self.chain = CARREFOUR
        self.offers = offers if offers is not None else [_offer(250000)]
        self.calls = 0
        self.fail: Exception | None = None

    async def search(self, term, *, store=None, sales_channel=None, limit=50):
        self.calls += 1
        if self.fail is not None:
            raise self.fail
        return self.offers[:limit]

    async def find_stores(self, postal_code):
        return []

    async def verify_prices(self, sku_quantities, *, store=None, sales_channel=None):
        self.calls += 1
        return self.offers

    async def aclose(self) -> None:
        return None

    def marca_propia(self) -> str:
        """Método fuera del Protocol, como el `search_by_ean` de Coto."""
        return "carrefour"


def _cached(provider, factory, *, ttl_s=3600, stale_max_s=86400):
    return CachedProvider(
        provider, ttl_s=ttl_s, stale_max_s=stale_max_s, session_factory=factory
    )


# --- acierto y vencimiento ------------------------------------------------


@pytest.mark.asyncio
async def test_segunda_busqueda_no_vuelve_a_pegarle_a_la_api(factory):
    provider = FakeProvider()
    cached = _cached(provider, factory)

    first = await cached.search("leche", limit=10)
    second = await cached.search("leche", limit=10)

    assert provider.calls == 1
    assert [o.offer.price_cents for o in second] == [o.offer.price_cents for o in first]
    assert first[0].freshness.from_cache is False
    assert second[0].freshness.from_cache is True
    assert second[0].freshness.stale is False


@pytest.mark.asyncio
async def test_pasado_el_ttl_se_vuelve_a_consultar(factory):
    provider = FakeProvider()
    cached = _cached(provider, factory, ttl_s=3600)
    await cached.search("leche", limit=10)

    # Se envejece la entrada en la base en vez de esperar una hora.
    async with factory()() as session:
        repo = PriceSearchCacheRepository(session)
        row = await repo.get(repo.key(CARREFOUR.slug, "leche", None, None))
        row.fetched_at = datetime.now(UTC) - timedelta(hours=2)
        await session.commit()

    provider.offers = [_offer(299000)]
    again = await cached.search("leche", limit=10)

    assert provider.calls == 2
    assert again[0].offer.price_cents == 299000
    assert again[0].freshness.from_cache is False


@pytest.mark.asyncio
async def test_el_termino_se_normaliza(factory):
    """'Leche Entera' y 'leche  entera' son el mismo pedido."""
    provider = FakeProvider()
    cached = _cached(provider, factory)

    await cached.search("Leche Entera", limit=10)
    await cached.search("leche  entera", limit=10)

    assert provider.calls == 1


@pytest.mark.asyncio
async def test_distinto_canal_no_comparte_entrada(factory):
    """El canal cambia el precio: colapsarlos serviría el de otra tienda."""
    provider = FakeProvider()
    cached = _cached(provider, factory)

    await cached.search("leche", sales_channel=1, limit=10)
    await cached.search("leche", sales_channel=3, limit=10)

    assert provider.calls == 2


@pytest.mark.asyncio
async def test_distinta_sucursal_no_comparte_entrada(factory):
    provider = FakeProvider()
    cached = _cached(provider, factory)
    store = Store(
        chain_slug=CARREFOUR.slug, external_id="carrefourar0026", name="Hiper Warnes"
    )

    await cached.search("leche", limit=10)
    await cached.search("leche", store=store, limit=10)

    assert provider.calls == 2


# --- el límite ------------------------------------------------------------


@pytest.mark.asyncio
async def test_una_entrada_grande_sirve_un_pedido_chico(factory):
    provider = FakeProvider([_offer(250000 + i, sku=f"s{i}") for i in range(50)])
    cached = _cached(provider, factory)

    await cached.search("leche", limit=50)
    recortado = await cached.search("leche", limit=20)

    assert provider.calls == 1
    assert len(recortado) == 20


@pytest.mark.asyncio
async def test_una_entrada_chica_no_sirve_un_pedido_grande(factory):
    """Devolver 20 donde se pidieron 50 haría perder al candidato que resuelve
    una línea de canasta."""
    provider = FakeProvider([_offer(250000 + i, sku=f"s{i}") for i in range(50)])
    cached = _cached(provider, factory)

    await cached.search("leche", limit=20)
    grande = await cached.search("leche", limit=50)

    assert provider.calls == 2
    assert len(grande) == 50


# --- frescura declarada ---------------------------------------------------


@pytest.mark.asyncio
async def test_la_edad_sale_de_cuando_se_consulto(factory):
    provider = FakeProvider()
    cached = _cached(provider, factory, ttl_s=6 * 3600)
    await cached.search("leche", limit=10)

    async with factory()() as session:
        repo = PriceSearchCacheRepository(session)
        row = await repo.get(repo.key(CARREFOUR.slug, "leche", None, None))
        row.fetched_at = datetime.now(UTC) - timedelta(hours=3)
        await session.commit()

    servido = await cached.search("leche", limit=10)

    assert servido[0].freshness.from_cache is True
    assert 3 * 3600 - 60 < servido[0].freshness.age_seconds <= 3 * 3600 + 60


@pytest.mark.asyncio
async def test_captured_at_no_se_pisa_al_servir_de_cache(factory):
    """Es cuándo rigió el precio. Pisarlo convertiría un dato viejo en nuevo."""
    provider = FakeProvider([_offer(250000, at=T0)])
    cached = _cached(provider, factory)

    await cached.search("leche", limit=10)
    servido = await cached.search("leche", limit=10)

    assert servido[0].offer.captured_at == T0


# --- respaldo de cadena caída ---------------------------------------------


@pytest.mark.asyncio
async def test_si_la_cadena_se_cae_se_sirve_el_ultimo_precio_marcado(factory):
    provider = FakeProvider()
    cached = _cached(provider, factory, ttl_s=0)  # todo vencido

    await cached.search("leche", limit=10)
    provider.fail = ProviderUnavailable("timeout")
    servido = await cached.search("leche", limit=10)

    assert servido[0].offer.price_cents == 250000
    assert servido[0].freshness.stale is True
    assert servido[0].freshness.from_cache is True


@pytest.mark.asyncio
async def test_sin_nada_guardado_el_fallo_se_propaga(factory):
    """Sin precio viejo no hay nada que degradar: el llamador tiene que enterarse."""
    provider = FakeProvider()
    provider.fail = ProviderUnavailable("timeout")
    cached = _cached(provider, factory)

    with pytest.raises(ProviderUnavailable):
        await cached.search("leche", limit=10)


@pytest.mark.asyncio
async def test_un_precio_demasiado_viejo_no_se_sirve_ni_como_respaldo(factory):
    provider = FakeProvider()
    cached = _cached(provider, factory, ttl_s=0, stale_max_s=3600)
    await cached.search("leche", limit=10)

    async with factory()() as session:
        repo = PriceSearchCacheRepository(session)
        row = await repo.get(repo.key(CARREFOUR.slug, "leche", None, None))
        row.fetched_at = datetime.now(UTC) - timedelta(days=30)
        await session.commit()

    provider.fail = ProviderUnavailable("timeout")
    with pytest.raises(ProviderUnavailable):
        await cached.search("leche", limit=10)


@pytest.mark.asyncio
async def test_el_respaldo_no_exige_el_limite_pedido(factory):
    """Menos candidatos es mejor que quedarse sin la cadena entera."""
    provider = FakeProvider([_offer(250000 + i, sku=f"s{i}") for i in range(50)])
    cached = _cached(provider, factory, ttl_s=0)
    await cached.search("leche", limit=20)

    provider.fail = ProviderUnavailable("timeout")
    servido = await cached.search("leche", limit=50)

    assert len(servido) == 20
    assert servido[0].freshness.stale is True


# --- bypass ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_fresh_ignora_la_cache(factory):
    provider = FakeProvider()
    cached = _cached(provider, factory)
    await cached.search("leche", limit=10)

    with bypass_cache():
        await cached.search("leche", limit=10)

    assert provider.calls == 2


@pytest.mark.asyncio
async def test_fresh_igual_conserva_el_respaldo(factory):
    """Pedir datos frescos es pedir que se intente ir a buscarlos, no pedir que
    la cadena desaparezca si falla."""
    provider = FakeProvider()
    cached = _cached(provider, factory)
    await cached.search("leche", limit=10)

    provider.fail = ProviderUnavailable("timeout")
    with bypass_cache():
        servido = await cached.search("leche", limit=10)

    assert servido[0].freshness.stale is True


# --- lo que no se cachea --------------------------------------------------


@pytest.mark.asyncio
async def test_verify_prices_nunca_se_cachea(factory):
    """Es el precio confirmado contra el checkout: uno de hace seis horas es una
    contradicción."""
    provider = FakeProvider()
    cached = _cached(provider, factory)

    await cached.verify_prices([("s1", 1)])
    await cached.verify_prices([("s1", 1)])

    assert provider.calls == 2


@pytest.mark.asyncio
async def test_delega_los_metodos_fuera_del_protocol(factory):
    """Coto expone `search_by_ean`; envolver no debería esconderlo."""
    cached = _cached(FakeProvider(), factory)
    assert cached.marca_propia() == "carrefour"


# --- interacción con el histórico ----------------------------------------


@pytest.mark.asyncio
async def test_lo_servido_de_cache_no_se_vuelve_a_guardar(factory):
    """Volver a guardarlo correría `last_seen_at` hacia atrás: el histórico
    diría que un precio dejó de verse antes de la última vez que se lo vio."""
    provider = FakeProvider()
    cached = _cached(provider, factory)

    live = await cached.search("leche", limit=10)
    servido = await cached.search("leche", limit=10)

    async with factory()() as session:
        service = IngestService(session)
        primero = await service.save_offers(cached, live)
        segundo = await service.save_offers(cached, servido)

    assert primero.snapshots_inserted == 1
    assert segundo.skus_seen == 0


# --- limpieza -------------------------------------------------------------


@pytest.mark.asyncio
async def test_purge_borra_solo_lo_que_ya_no_sirve_de_respaldo(factory, monkeypatch):
    from app.services.providers import cache as cache_module

    monkeypatch.setattr(cache_module.settings, "PRICE_CACHE_STALE_MAX_S", 3600)
    provider = FakeProvider()
    cached = _cached(provider, factory)
    await cached.search("leche", limit=10)
    await cached.search("pan", limit=10)

    async with factory()() as session:
        repo = PriceSearchCacheRepository(session)
        row = await repo.get(repo.key(CARREFOUR.slug, "pan", None, None))
        row.fetched_at = datetime.now(UTC) - timedelta(days=30)
        await session.commit()

    removed = await purge_expired(factory)

    assert removed == 1
    async with factory()() as session:
        repo = PriceSearchCacheRepository(session)
        assert await repo.get(repo.key(CARREFOUR.slug, "leche", None, None)) is not None
        assert await repo.get(repo.key(CARREFOUR.slug, "pan", None, None)) is None
