"""Caché de precios: una capa entre la app y las cadenas.

Envuelve a un proveedor en lugar de vivir dentro de `search_all` porque los
caminos que consultan precios son varios —`/search`, `/compare` y el fan-out de
canastas, que es el caro: una lista de 15 líneas contra 4 cadenas son 60
requests— y ninguno pasa por el otro. Envolviendo el proveedor, los tres heredan
la caché sin tocar una línea, y las cadenas que vengan (Día, Coto) quedan
cubiertas por construcción: `PriceProvider` es un `Protocol`, así que esto no
sabe ni le importa si adentro hay VTEX o un scraper.

Resuelve dos problemas distintos con la misma tabla:

1. **No repetir el request.** Dentro del TTL se contesta con lo guardado.
2. **No perder una cadena caída.** Vencido el TTL, si la cadena no responde se
   sirve igual el último precio conocido, marcado `stale`. Perder Disco entero
   porque su API tardó 20 segundos deja al usuario sin la comparación que sí
   podíamos darle.

Lo que **nunca** se cachea es `verify_prices` y `simulate_basket`: son el precio
confirmado contra el checkout, el número con el que alguien decide dónde
comprar. Un total autoritativo de hace seis horas es una contradicción.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import settings
from app.core.db import get_session_factory
from app.db.repositories import PriceSearchCacheRepository
from app.db.tables import as_aware
from app.models.catalog import (
    BasketSimulation,
    CategoryNode,
    Chain,
    Freshness,
    ProductOffer,
    Store,
)
from app.services.providers.base import PriceProvider

logger = logging.getLogger(__name__)

SessionFactory = Callable[[], async_sessionmaker[AsyncSession]]

_bypass: ContextVar[bool] = ContextVar("price_cache_bypass", default=False)


@contextlib.contextmanager
def bypass_cache(active: bool = True) -> Iterator[None]:
    """Dentro de este bloque se consulta siempre en vivo.

    Va por `ContextVar` y no por un parámetro de `search()` a propósito: agregar
    un `fresh=` al Protocol obligaría a Coto, a VTEX y a cada cadena futura a
    aceptar y arrastrar un argumento sobre una caché que no saben que existe. El
    contexto se copia solo a las tareas de `asyncio.gather`, así que el pedido de
    un endpoint alcanza a las N búsquedas en paralelo que dispara.

    Sigue valiendo como respaldo: si la cadena no responde igual se sirve lo
    guardado marcado `stale`. Pedir datos frescos es pedir que se intente ir a
    buscarlos, no pedir que la cadena desaparezca de la comparación si falla.
    """
    token = _bypass.set(active)
    try:
        yield
    finally:
        _bypass.reset(token)


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(value: datetime) -> datetime:
    """SQLite devuelve los `DateTime(timezone=True)` sin tzinfo.

    Restarle un naive a un aware tira `TypeError`, y acá esa resta es justo la
    que decide si el precio venció. La implementación vive en `db.tables` porque
    el problema es del almacenamiento y lo sufre también la expiración de
    sesiones.
    """
    return as_aware(value)


class CachedProvider:
    """Un `PriceProvider` con caché de búsquedas por delante.

    Cumple el mismo Protocol que el proveedor que envuelve, así que el registry
    lo puede sustituir sin que nadie se entere.
    """

    def __init__(
        self,
        inner: PriceProvider,
        *,
        ttl_s: int | None = None,
        stale_max_s: int | None = None,
        session_factory: SessionFactory | None = None,
    ) -> None:
        self._inner = inner
        self._ttl = timedelta(seconds=ttl_s if ttl_s is not None else settings.PRICE_CACHE_TTL_S)
        self._stale_max = timedelta(
            seconds=stale_max_s if stale_max_s is not None else settings.PRICE_CACHE_STALE_MAX_S
        )
        self._session_factory = session_factory or get_session_factory

    def __getattr__(self, name: str):
        """Delega lo que no envolvemos.

        Algunos proveedores tienen métodos fuera del Protocol —Coto expone
        `search_by_ean`— y envolverlos no debería esconderlos. `object.__getattribute__`
        y no `self._inner`: si el atributo se pide antes de que `__init__` lo
        asigne, esto último se llamaría a sí mismo hasta el stack overflow.
        """
        return getattr(object.__getattribute__(self, "_inner"), name)

    @property
    def chain(self) -> Chain:
        return self._inner.chain

    # --- caché ------------------------------------------------------------

    async def _lookup(self, key: str):
        """Lee la entrada. Un fallo de la caché nunca rompe la búsqueda."""
        try:
            async with self._session_factory()() as session:
                row = await PriceSearchCacheRepository(session).get(key)
                if row is None:
                    return None
                # Se copian los campos antes de cerrar la sesión: la fila queda
                # detached y tocarla después dispararía un lazy load sin sesión.
                return (_aware(row.fetched_at), int(row.limit), list(row.offers or ()))
        except Exception as exc:  # noqa: BLE001 - degradar a consulta en vivo
            logger.warning("No se pudo leer la caché de %s: %s", self.chain.slug, exc)
            return None

    async def _store(
        self,
        key: str,
        *,
        term: str,
        sales_channel: int | None,
        store_key: str | None,
        limit: int,
        offers: Sequence[ProductOffer],
        fetched_at: datetime,
    ) -> None:
        """Guarda el resultado. Si falla, se pierde el ahorro, no la respuesta."""
        try:
            async with self._session_factory()() as session:
                await PriceSearchCacheRepository(session).put(
                    key,
                    chain_slug=self.chain.slug,
                    term=term,
                    sales_channel=sales_channel,
                    store_key=store_key,
                    limit=limit,
                    # `freshness` no se guarda: describe *esta* respuesta, no el
                    # precio. Guardarlo devolvería `age_seconds=0` para siempre.
                    offers=[
                        o.model_dump(mode="json", exclude={"freshness"}) for o in offers
                    ],
                    fetched_at=fetched_at,
                )
                await session.commit()
        except Exception as exc:  # noqa: BLE001 - degradar, no romper
            logger.warning("No se pudo cachear %s en %s: %s", term, self.chain.slug, exc)

    def _revive(
        self,
        entry: tuple[datetime, int, list],
        limit: int,
        now: datetime,
        *,
        stale: bool,
    ) -> list[ProductOffer]:
        """Reconstruye las ofertas guardadas con su frescura real.

        `captured_at` queda como estaba —es cuándo rigió el precio—, y la
        antigüedad va aparte en `freshness`. Pisarlo con la hora actual sería
        convertir un dato viejo en uno recién traído con solo servirlo.
        """
        fetched_at, _, raw_offers = entry
        freshness = Freshness(
            fetched_at=fetched_at,
            age_seconds=max(0, int((now - fetched_at).total_seconds())),
            from_cache=True,
            stale=stale,
        )
        return [
            ProductOffer.model_validate(raw).model_copy(update={"freshness": freshness})
            for raw in raw_offers[:limit]
        ]

    async def search(
        self,
        term: str,
        *,
        store: Store | None = None,
        sales_channel: int | None = None,
        limit: int = 50,
    ) -> list[ProductOffer]:
        store_key = store.key if store is not None else None
        key = PriceSearchCacheRepository.key(
            self.chain.slug, term, sales_channel, store_key
        )
        now = _now()

        # Se busca siempre, incluso con bypass: aunque no se sirva como acierto,
        # es el respaldo si la cadena no contesta.
        entry = await self._lookup(key)

        if entry is not None and not _bypass.get():
            fetched_at, cached_limit, _ = entry
            # `cached_limit >= limit`: una entrada de 20 no puede contestar un
            # pedido de 50 sin inventar los 30 que faltan, y devolver de menos
            # haría que una línea de canasta pierda al candidato que la resuelve.
            if cached_limit >= limit and now - fetched_at <= self._ttl:
                return self._revive(entry, limit, now, stale=False)

        try:
            offers = await self._inner.search(
                term, store=store, sales_channel=sales_channel, limit=limit
            )
        except Exception as exc:  # noqa: BLE001 - se decide abajo si se degrada
            if entry is not None and now - entry[0] <= self._stale_max:
                # Acá no se exige `cached_limit >= limit`: es el camino
                # degradado, y menos candidatos de los pedidos sigue siendo
                # mejor que quedarse sin la cadena entera.
                logger.warning(
                    "%s no respondió '%s' (%s); se sirve el precio de hace %s",
                    self.chain.slug,
                    term,
                    exc,
                    now - entry[0],
                )
                return self._revive(entry, limit, now, stale=True)
            raise

        fetched = Freshness(
            fetched_at=now, age_seconds=0, from_cache=False, stale=False
        )
        await self._store(
            key,
            term=term,
            sales_channel=sales_channel,
            store_key=store_key,
            limit=limit,
            offers=offers,
            fetched_at=now,
        )
        return [o.model_copy(update={"freshness": fetched}) for o in offers]

    # --- sin caché --------------------------------------------------------

    async def find_stores(self, postal_code: str) -> list[Store]:
        return await self._inner.find_stores(postal_code)

    def iter_category(
        self,
        category: CategoryNode,
        *,
        store: Store | None = None,
        sales_channel: int | None = None,
    ) -> AsyncIterator[ProductOffer]:
        """Sin caché: un crawl de catálogo va a buscar precios nuevos, y
        cachearlo llenaría la tabla con miles de términos que nadie va a repetir."""
        return self._inner.iter_category(
            category, store=store, sales_channel=sales_channel
        )

    async def category_tree(self, depth: int = 3) -> tuple[CategoryNode, ...]:
        return await self._inner.category_tree(depth)

    async def verify_prices(
        self,
        sku_quantities: Sequence[tuple[str, int]],
        *,
        store: Store | None = None,
        sales_channel: int | None = None,
    ) -> list[ProductOffer]:
        """Nunca cacheado: es el precio confirmado contra el checkout."""
        return await self._inner.verify_prices(
            sku_quantities, store=store, sales_channel=sales_channel
        )

    async def simulate_basket(
        self,
        sku_quantities: Sequence[tuple[str, int]],
        *,
        store: Store | None = None,
        sales_channel: int | None = None,
        card_bin: str | None = None,
    ) -> BasketSimulation | None:
        """Nunca cacheado: mismo motivo que `verify_prices`."""
        return await self._inner.simulate_basket(
            sku_quantities,
            store=store,
            sales_channel=sales_channel,
            card_bin=card_bin,
        )

    async def aclose(self) -> None:
        await self._inner.aclose()


async def purge_expired(session_factory: SessionFactory | None = None) -> int:
    """Saca de la tabla lo que ya no sirve ni como respaldo.

    El corte va por `PRICE_CACHE_STALE_MAX_S` y no por el TTL: entre los dos, una
    entrada está vencida pero todavía es lo que se sirve si la cadena se cae.
    """
    factory = session_factory or get_session_factory
    cutoff = _now() - timedelta(seconds=settings.PRICE_CACHE_STALE_MAX_S)
    try:
        async with factory()() as session:
            removed = await PriceSearchCacheRepository(session).purge(cutoff)
            await session.commit()
    except Exception as exc:  # noqa: BLE001 - es higiene, no una precondición
        logger.warning("No se pudo limpiar la caché de precios: %s", exc)
        return 0
    if removed:
        logger.info("Caché de precios: %d entradas vencidas eliminadas", removed)
    return removed
