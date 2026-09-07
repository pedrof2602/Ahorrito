"""Acceso a datos. Sin lógica de negocio: reciben la sesión, no la crean, para
que un servicio pueda componer varios repositorios en una sola transacción.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import catalog as domain
from app.models import payment as pay
from app.services.locations import Location, normalize_query

from . import tables as t


def promo_digest(promotions: tuple[domain.Promotion, ...]) -> str:
    """Huella estable de un conjunto de promociones.

    Se ordena antes de hashear: VTEX no garantiza el orden, y sin ordenar el
    mismo conjunto produciría huellas distintas y falsos cambios de precio.
    """
    if not promotions:
        return ""
    parts = sorted(f"{p.name}|{p.kind}|{p.percent}" for p in promotions)
    return hashlib.sha256("||".join(parts).encode()).hexdigest()[:32]


@dataclass(slots=True)
class IngestResult:
    skus_seen: int = 0
    snapshots_inserted: int = 0
    snapshots_touched: int = 0
    """Precios sin cambios: solo se actualizó `last_seen_at`."""


class ChainRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def upsert(self, chain: domain.Chain) -> t.Chain:
        row = await self._s.scalar(select(t.Chain).where(t.Chain.slug == chain.slug))
        if row is None:
            row = t.Chain(slug=chain.slug)
            self._s.add(row)
        row.display_name = chain.display_name
        row.supports_store_prices = chain.supports_store_prices
        await self._s.flush()
        return row

    async def by_slug(self, slug: str) -> t.Chain | None:
        return await self._s.scalar(select(t.Chain).where(t.Chain.slug == slug))

    async def by_id(self, chain_id: int) -> t.Chain | None:
        return await self._s.get(t.Chain, chain_id)

    async def all(self) -> list[t.Chain]:
        return list(await self._s.scalars(select(t.Chain).order_by(t.Chain.display_name)))


class StoreRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def upsert(self, chain_id: int, store: domain.Store) -> t.Store:
        row = await self._s.scalar(
            select(t.Store).where(
                t.Store.chain_id == chain_id, t.Store.external_id == store.external_id
            )
        )
        if row is None:
            row = t.Store(chain_id=chain_id, external_id=store.external_id)
            self._s.add(row)
        row.name = store.name
        row.sales_channel = store.sales_channel
        row.region_id = store.region_id
        row.postal_code = store.postal_code
        await self._s.flush()
        return row

    async def by_postal_code(self, postal_code: str) -> list[t.Store]:
        return list(
            await self._s.scalars(
                select(t.Store).where(t.Store.postal_code == postal_code)
            )
        )

    async def by_id(self, store_id: int) -> t.Store | None:
        return await self._s.get(t.Store, store_id)

    async def upsert_location(
        self, chain_id: int, location: Location, now: datetime | None = None
    ) -> t.Store:
        """Crea o actualiza una sucursal a partir de una ubicación descubierta.

        Las sucursales de las que hay ubicación **no son las mismas** de las que
        hay precio: Carrefour publica 390 puntos y el catálogo de precios conoce
        7, y en Coto los IDs ni siquiera se superponen del todo. Por eso esto
        inserta si no existe en vez de exigir que la fila ya esté: el mapa tiene
        que poder mostrar una sucursal aunque el comparador nunca le haya pedido
        un precio.
        """
        row = await self._s.scalar(
            select(t.Store).where(
                t.Store.chain_id == chain_id,
                t.Store.external_id == location.external_id,
            )
        )
        if row is None:
            row = t.Store(
                chain_id=chain_id,
                external_id=location.external_id,
                name=location.name,
            )
            self._s.add(row)
        # El nombre NO se pisa si ya había uno: el del catálogo de precios es el
        # que usa el resto de la app, y el del localizador puede diferir.
        elif not row.name:
            row.name = location.name

        row.latitude = location.latitude
        row.longitude = location.longitude
        row.address = location.address
        row.city = location.city
        row.province = location.province
        row.location_postal_code = location.postal_code
        row.location_source = location.source
        row.located_at = now or t.utcnow()
        await self._s.flush()
        return row

    async def located_in_postal_code(self, postal_code: str) -> list[t.Store]:
        """Sucursales ubicadas *dentro* de un CP.

        Es el ancla que ubica al usuario sin geocodificar nada: si hay sucursales
        en su código postal, el promedio de sus coordenadas es un centro de mapa
        más que suficiente, y sale de datos que ya están en la base.
        """
        return list(
            await self._s.scalars(
                select(t.Store).where(
                    t.Store.location_postal_code == postal_code,
                    t.Store.latitude.is_not(None),
                    t.Store.longitude.is_not(None),
                )
            )
        )

    async def located(self, chain_slug: str | None = None) -> list[t.Store]:
        """Sucursales que se pueden poner en un mapa."""
        query = select(t.Store).where(
            t.Store.latitude.is_not(None), t.Store.longitude.is_not(None)
        )
        if chain_slug is not None:
            query = query.join(t.Chain).where(t.Chain.slug == chain_slug)
        return list(await self._s.scalars(query))


class GeocodeCacheRepository:
    """Direcciones ya resueltas, para no volver a preguntarle a Nominatim."""

    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def get(self, query: str) -> t.GeocodeCache | None:
        return await self._s.get(t.GeocodeCache, normalize_query(query))

    async def put(
        self,
        query: str,
        latitude: float | None,
        longitude: float | None,
        display_name: str | None = None,
    ) -> t.GeocodeCache:
        """Guarda el resultado, **incluso si no resolvió**.

        Una dirección que Nominatim no conoce se guarda con las coordenadas en
        null. Sin eso se reintentaría en cada corrida, gastando el segundo de
        espera que exige su política de uso para volver a no obtener nada.
        """
        key = normalize_query(query)
        row = await self._s.get(t.GeocodeCache, key)
        if row is None:
            row = t.GeocodeCache(query=key)
            self._s.add(row)
        row.latitude = latitude
        row.longitude = longitude
        row.display_name = display_name
        row.resolved_at = t.utcnow()
        await self._s.flush()
        return row


class ProductRepository:
    """Productos canónicos y su vínculo con los SKU de cada cadena."""

    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def link_by_ean(self, product: domain.Product) -> t.Product | None:
        """Resuelve el producto canónico por EAN.

        Sin EAN confiable no se vincula: fusionar por nombre sin umbral haría
        que dos productos distintos compartan ficha y le mostraríamos al usuario
        el precio de otra cosa. Es preferible dejarlo suelto.
        """
        if not product.ean:
            return None
        row = await self._s.scalar(select(t.Product).where(t.Product.ean == product.ean))
        if row is None:
            row = t.Product(ean=product.ean, name=product.name, brand=product.brand)
            self._s.add(row)
            await self._s.flush()
        return row

    async def by_ean(self, ean: str) -> t.Product | None:
        return await self._s.scalar(select(t.Product).where(t.Product.ean == ean))

    async def upsert_sku(
        self, chain_id: int, product: domain.Product, observed_at: datetime
    ) -> t.ChainSku:
        # El producto canónico se resuelve ANTES de crear la fila: `link_by_ean`
        # hace un select, y ese select dispara un autoflush de la fila a medio
        # poblar, que viola el NOT NULL de external_product_id.
        canonical = await self.link_by_ean(product)

        row = await self._s.scalar(
            select(t.ChainSku).where(
                t.ChainSku.chain_id == chain_id, t.ChainSku.sku_id == product.sku_id
            )
        )
        if row is None:
            row = t.ChainSku(
                chain_id=chain_id,
                sku_id=product.sku_id,
                external_product_id=product.product_id,
                name=product.name,
                first_seen_at=observed_at,
            )
            self._s.add(row)
        row.product_id = canonical.id if canonical else None
        row.external_product_id = product.product_id
        row.name = product.name
        row.brand = product.brand
        row.ean = product.ean
        row.image_url = product.image_url
        row.link = product.link
        row.measurement_unit = product.measurement_unit
        row.unit_multiplier = product.unit_multiplier
        row.category_path = list(product.category_path)
        row.last_seen_at = observed_at
        await self._s.flush()
        return row

    async def skus_for_ean(self, ean: str) -> list[t.ChainSku]:
        return list(await self._s.scalars(select(t.ChainSku).where(t.ChainSku.ean == ean)))

    async def search(self, term: str, limit: int = 50) -> list[t.ChainSku]:
        pattern = f"%{term.strip()}%"
        return list(
            await self._s.scalars(
                select(t.ChainSku).where(t.ChainSku.name.ilike(pattern)).limit(limit)
            )
        )


class PriceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def latest(
        self, chain_sku_id: int, store_id: int | None
    ) -> t.PriceSnapshot | None:
        return await self._s.scalar(
            select(t.PriceSnapshot)
            .where(
                t.PriceSnapshot.chain_sku_id == chain_sku_id,
                t.PriceSnapshot.store_id.is_(store_id)
                if store_id is None
                else t.PriceSnapshot.store_id == store_id,
            )
            .order_by(t.PriceSnapshot.observed_at.desc())
            .limit(1)
        )

    async def record(
        self, chain_sku_id: int, store_id: int | None, offer: domain.Offer
    ) -> bool:
        """Guarda el precio si cambió. Devuelve True si insertó una fila nueva.

        Si nada cambió, solo corre `last_seen_at`: así el histórico queda como
        una tabla de cambios de precio en lugar de un volcado por corrida.
        """
        digest = promo_digest(offer.promotions)
        previous = await self.latest(chain_sku_id, store_id)

        unchanged = (
            previous is not None
            and previous.price_cents == offer.price_cents
            and previous.reference_price_cents == offer.reference_price_cents
            and previous.available == offer.available
            and previous.promo_digest == digest
        )
        if unchanged:
            previous.last_seen_at = offer.captured_at
            await self._s.flush()
            return False

        self._s.add(
            t.PriceSnapshot(
                chain_sku_id=chain_sku_id,
                store_id=store_id,
                seller_id=offer.seller_id,
                price_cents=offer.price_cents,
                reference_price_cents=offer.reference_price_cents,
                price_per_unit_cents=offer.price_per_unit_cents,
                available=offer.available,
                available_quantity=offer.available_quantity,
                price_scope=str(offer.price_scope),
                promotions=[p.model_dump(mode="json") for p in offer.promotions],
                promo_digest=digest,
                observed_at=offer.captured_at,
                last_seen_at=offer.captured_at,
            )
        )
        await self._s.flush()
        return True

    async def history(
        self, chain_sku_id: int, since: datetime, store_id: int | None = None
    ) -> list[t.PriceSnapshot]:
        stmt = select(t.PriceSnapshot).where(
            t.PriceSnapshot.chain_sku_id == chain_sku_id,
            t.PriceSnapshot.observed_at >= since,
        )
        if store_id is not None:
            stmt = stmt.where(t.PriceSnapshot.store_id == store_id)
        return list(
            await self._s.scalars(stmt.order_by(t.PriceSnapshot.observed_at.asc()))
        )


class PriceSearchCacheRepository:
    """Búsquedas ya contestadas por una cadena, para no volver a preguntarlas."""

    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    @staticmethod
    def key(
        chain_slug: str,
        term: str,
        sales_channel: int | None,
        store_key: str | None,
    ) -> str:
        """Identidad de una búsqueda.

        El término se normaliza —minúsculas, espacios colapsados— porque "Leche
        Entera" y "leche  entera" son el mismo pedido y partirlos en dos filas
        duplicaría los requests que esta tabla existe para evitar.

        El canal y la sucursal **sí** entran: son exactamente lo que hace que la
        misma consulta devuelva precios distintos (Carrefour publica la misma
        leche a $2249 en el canal 1 y a $2999 en el 3). Colapsarlos serviría el
        precio de otra tienda con cara de acierto de caché.
        """
        normalized = " ".join(term.lower().split())
        raw = f"{chain_slug}|{normalized}|{sales_channel or ''}|{store_key or ''}"
        return hashlib.sha256(raw.encode()).hexdigest()

    async def get(self, key: str) -> t.PriceSearchCache | None:
        return await self._s.get(t.PriceSearchCache, key)

    async def put(
        self,
        key: str,
        *,
        chain_slug: str,
        term: str,
        sales_channel: int | None,
        store_key: str | None,
        limit: int,
        offers: list[dict],
        fetched_at: datetime,
    ) -> t.PriceSearchCache:
        row = await self._s.get(t.PriceSearchCache, key)
        if row is None:
            row = t.PriceSearchCache(key=key)
            self._s.add(row)
        row.chain_slug = chain_slug
        row.term = term
        row.sales_channel = sales_channel
        row.store_key = store_key
        row.limit = limit
        row.offers = offers
        row.fetched_at = fetched_at
        await self._s.flush()
        return row

    async def purge(self, older_than: datetime) -> int:
        """Borra las entradas que ya no sirven ni como último recurso.

        Una entrada vencida todavía vale —es el precio que se sirve cuando la
        cadena no responde—, así que el corte va por `PRICE_CACHE_STALE_MAX_S`,
        no por el TTL. Devuelve cuántas borró.
        """
        result = await self._s.execute(
            delete(t.PriceSearchCache).where(t.PriceSearchCache.fetched_at < older_than)
        )
        await self._s.flush()
        return result.rowcount or 0


def to_domain_instrument(row: t.PaymentInstrument) -> pay.PaymentInstrument:
    """Fila -> modelo. Valida de paso: los BIN se normalizan al reconstruir."""
    return pay.PaymentInstrument(
        id=row.id,
        label=row.label,
        issuer_slug=row.issuer_slug,
        brand=row.brand,
        kind=pay.CardKind(row.kind),
        rails=frozenset(pay.PaymentRail(r) for r in (row.rails or [])),
        bins=tuple(row.bins or []),
        bin_source=pay.BinSource(row.bin_source),
    )


class PaymentInstrumentRepository:
    """Medios de pago del usuario."""

    def __init__(self, session: AsyncSession, user_id: int) -> None:
        self._s = session
        self._uid = user_id

    async def list(self) -> list[t.PaymentInstrument]:
        return list(
            await self._s.scalars(
                select(t.PaymentInstrument)
                .where(t.PaymentInstrument.user_id == self._uid)
                .order_by(t.PaymentInstrument.label)
            )
        )

    async def by_id(
        self, instrument_id: int
    ) -> t.PaymentInstrument | None:
        row = await self._s.get(t.PaymentInstrument, instrument_id)
        return row if row is not None and row.user_id == self._uid else None

    async def create(
        self, instrument: pay.PaymentInstrument
    ) -> t.PaymentInstrument:
        row = t.PaymentInstrument(
            user_id=self._uid,
            label=instrument.label,
            issuer_slug=instrument.issuer_slug,
            brand=instrument.brand,
            kind=str(instrument.kind),
            rails=[str(r) for r in sorted(instrument.rails)],
            bins=list(instrument.bins),
            bin_source=str(instrument.bin_source),
        )
        self._s.add(row)
        await self._s.flush()
        return row

    async def update(
        self, row: t.PaymentInstrument, changes: dict[str, object]
    ) -> t.PaymentInstrument:
        for field, value in changes.items():
            setattr(row, field, value)
        await self._s.flush()
        return row

    async def delete(self, row: t.PaymentInstrument) -> None:
        await self._s.delete(row)
        await self._s.flush()

    async def mark_verified(
        self,
        row: t.PaymentInstrument,
        promotions: list[str],
        when: datetime,
    ) -> t.PaymentInstrument:
        """Deja constancia de qué promos activó el BIN contra la API real.

        Un BIN que no activó nada **no** pasa a `verified`: pudo no calificar, o
        estar mal cargado, y no hay forma de distinguirlo desde acá. Marcarlo
        igual convertiría "no pude comprobarlo" en "lo comprobé".
        """
        row.verified_promotions = promotions
        row.verified_at = when
        if promotions:
            row.bin_source = str(pay.BinSource.VERIFIED)
        await self._s.flush()
        return row


class PromoUsageRepository:
    """Consumo de los topes, por regla y ventana."""

    def __init__(self, session: AsyncSession, user_id: int) -> None:
        self._s = session
        self._uid = user_id

    async def used_cents(
        self, rule_id: str, period_start: date
    ) -> int:
        row = await self._s.scalar(
            select(t.PromoUsage).where(
                t.PromoUsage.user_id == self._uid,
                t.PromoUsage.rule_id == rule_id,
                t.PromoUsage.period_start == period_start,
            )
        )
        return row.used_cents if row else 0

    async def usage_map(
        self,
        rules: list[tuple[str, date]],
    ) -> dict[str, int]:
        """`{rule_id: consumido}` para las ventanas vigentes de cada regla."""
        return {
            rule_id: await self.used_cents(rule_id, period_start)
            for rule_id, period_start in rules
        }

    async def add(
        self,
        rule_id: str,
        period_start: date,
        amount_cents: int,
        now: datetime | None = None,
    ) -> t.PromoUsage:
        row = await self._s.scalar(
            select(t.PromoUsage).where(
                t.PromoUsage.user_id == self._uid,
                t.PromoUsage.rule_id == rule_id,
                t.PromoUsage.period_start == period_start,
            )
        )
        if row is None:
            row = t.PromoUsage(
                user_id=self._uid,
                rule_id=rule_id,
                period_start=period_start,
                used_cents=0,
            )
            self._s.add(row)
        row.used_cents += amount_cents
        row.updated_at = now or t.utcnow()
        await self._s.flush()
        return row


class UserProfileRepository:
    """El perfil del usuario. Una sola fila, creada al vuelo."""

    def __init__(self, session: AsyncSession, user_id: int) -> None:
        self._s = session
        self._uid = user_id

    async def get_or_create(self) -> t.UserProfile:
        """Devuelve el perfil, creándolo con defaults si todavía no existe.

        Sin esto, cada endpoint tendría que distinguir "no hay perfil" de "hay
        uno vacío", y son lo mismo: una app recién instalada.
        """
        row = await self._s.get(t.UserProfile, self._uid)
        if row is None:
            row = t.UserProfile(user_id=self._uid)
            self._s.add(row)
            await self._s.flush()
        return row

    async def update(
        self, row: t.UserProfile, changes: dict[str, object]
    ) -> t.UserProfile:
        for field, value in changes.items():
            setattr(row, field, value)
        row.updated_at = t.utcnow()
        await self._s.flush()
        return row


class AddressRepository:
    """Domicilios guardados."""

    def __init__(self, session: AsyncSession, user_id: int) -> None:
        self._s = session
        self._uid = user_id

    async def list(self) -> list[t.Address]:
        return list(
            await self._s.scalars(
                select(t.Address)
                .where(t.Address.user_id == self._uid)
                # El predeterminado primero: es el que se usa casi siempre.
                .order_by(t.Address.is_default.desc(), t.Address.label)
            )
        )

    async def by_id(
        self, address_id: int
    ) -> t.Address | None:
        row = await self._s.get(t.Address, address_id)
        return row if row is not None and row.user_id == self._uid else None

    async def default_located(
        self
    ) -> t.Address | None:
        """El domicilio desde el que se miden las distancias.

        El predeterminado si está ubicado; si no, cualquiera que lo esté. Esto
        último es a propósito: alguien que ubicó *un* domicilio quiere que el
        mapa lo use, y exigirle además que lo marque como principal sería
        pedirle dos pasos para expresar una sola intención.
        """
        located = [
            row
            for row in await self.list()
            if row.latitude is not None and row.longitude is not None
        ]
        if not located:
            return None
        # `list` ya ordena con el predeterminado primero.
        return located[0]

    async def create(
        self, fields: dict[str, object]
    ) -> t.Address:
        row = t.Address(user_id=self._uid, **fields)
        self._s.add(row)
        await self._s.flush()
        return row

    async def update(self, row: t.Address, changes: dict[str, object]) -> t.Address:
        for field, value in changes.items():
            setattr(row, field, value)
        await self._s.flush()
        return row

    async def delete(self, row: t.Address) -> None:
        await self._s.delete(row)
        await self._s.flush()

    async def clear_default(
        self, except_id: int | None = None
    ) -> None:
        """Deja un solo domicilio predeterminado.

        "Predeterminado" es una propiedad del conjunto, no de la fila: marcar uno
        implica desmarcar el resto. Hacerlo acá y no en el endpoint evita que el
        que se olvide de llamarlo deje dos marcados y la UI elija por orden.
        """
        for row in await self.list():
            if row.id != except_id and row.is_default:
                row.is_default = False
        await self._s.flush()


class CustomStoreRepository:
    """Sucursales cargadas a mano."""

    def __init__(self, session: AsyncSession, user_id: int) -> None:
        self._s = session
        self._uid = user_id

    async def list(self) -> list[t.CustomStore]:
        return list(
            await self._s.scalars(
                select(t.CustomStore)
                .where(t.CustomStore.user_id == self._uid)
                .order_by(t.CustomStore.name)
            )
        )

    async def by_id(
        self, store_id: int
    ) -> t.CustomStore | None:
        row = await self._s.get(t.CustomStore, store_id)
        return row if row is not None and row.user_id == self._uid else None

    async def create(
        self, fields: dict[str, object]
    ) -> t.CustomStore:
        row = t.CustomStore(user_id=self._uid, **fields)
        self._s.add(row)
        await self._s.flush()
        return row

    async def update(
        self, row: t.CustomStore, changes: dict[str, object]
    ) -> t.CustomStore:
        for field, value in changes.items():
            setattr(row, field, value)
        await self._s.flush()
        return row

    async def delete(self, row: t.CustomStore) -> None:
        await self._s.delete(row)
        await self._s.flush()


class ShoppingListRepository:
    """Listas de compras."""

    DEFAULT_NAME = "Mi lista"
    MAX_LINES = 50
    """El backend acepta 50 líneas por canasta; guardar más sería guardar algo
    que después la comparación rechaza."""

    def __init__(self, session: AsyncSession, user_id: int) -> None:
        self._s = session
        self._uid = user_id

    async def list(self) -> list[t.ShoppingList]:
        return list(
            await self._s.scalars(
                select(t.ShoppingList)
                .where(t.ShoppingList.user_id == self._uid)
                .order_by(t.ShoppingList.updated_at.desc())
            )
        )

    async def by_id(
        self, list_id: int
    ) -> t.ShoppingList | None:
        row = await self._s.get(t.ShoppingList, list_id)
        return row if row is not None and row.user_id == self._uid else None

    async def create(
        self, name: str
    ) -> t.ShoppingList:
        # `lines=[]` explícito y no por omisión: sin eso la colección queda sin
        # cargar y el primer `row.lines` después del flush intenta ir a la base
        # fuera del contexto async, que con `AsyncSession` es `MissingGreenlet`.
        row = t.ShoppingList(user_id=self._uid, name=name, lines=[])
        self._s.add(row)
        await self._s.flush()
        return row

    async def default_list(self) -> t.ShoppingList:
        """La lista con la que trabaja la pantalla principal.

        Es la más reciente si hay alguna, y una nueva si no hay ninguna: el
        frontend no tiene selector de listas todavía, así que necesita una a la
        que apuntar sin preguntar.
        """
        existing = await self.list()
        if existing:
            return existing[0]
        return await self.create(self.DEFAULT_NAME)

    async def replace_lines(
        self, row: t.ShoppingList, lines: list[dict[str, object]]
    ) -> t.ShoppingList:
        """Reemplaza las líneas enteras y renumera `position`.

        Reemplazo total y no diff: la lista tiene 50 líneas como techo, el
        frontend siempre manda el estado completo, y diffear del lado del
        servidor solo agregaría una forma de que las dos puntas discrepen.
        """
        row.lines.clear()
        for position, line in enumerate(lines[: self.MAX_LINES]):
            row.lines.append(t.ShoppingListLine(position=position, **line))
        row.updated_at = t.utcnow()
        await self._s.flush()
        return row

    async def delete(self, row: t.ShoppingList) -> None:
        await self._s.delete(row)
        await self._s.flush()


# ------------------------------------------------------------ autenticación


class UserRepository:
    """Cuentas.

    Es de los pocos repositorios que **no** lleva `user_id` en el constructor:
    su trabajo empieza antes de que exista un usuario autenticado.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def by_email(self, email: str) -> t.User | None:
        """Busca por email ya normalizado. Normalizar acá adentro escondería que
        el llamador tiene que hacerlo también al crear, y las dos puntas tienen
        que coincidir o el índice único no sirve de nada."""
        return await self._s.scalar(select(t.User).where(t.User.email == email))

    async def by_id(self, user_id: int) -> t.User | None:
        return await self._s.get(t.User, user_id)

    async def count(self) -> int:
        return await self._s.scalar(select(func.count()).select_from(t.User)) or 0

    async def create(
        self, email: str, password_hash: str, *, role: str = "user"
    ) -> t.User:
        row = t.User(email=email, password_hash=password_hash, role=role)
        self._s.add(row)
        await self._s.flush()
        return row


class AuthSessionRepository:
    """Sesiones abiertas. Se direccionan por el hash del token, nunca por el
    token: lo que entra por la cookie se hashea antes de tocar la base."""

    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def create(
        self,
        user_id: int,
        token_hash: str,
        *,
        expires_at: datetime,
        user_agent: str | None = None,
    ) -> t.AuthSession:
        row = t.AuthSession(
            user_id=user_id,
            token_hash=token_hash,
            expires_at=expires_at,
            user_agent=(user_agent or None) and user_agent[:300],
        )
        self._s.add(row)
        await self._s.flush()
        return row

    async def by_token_hash(self, token_hash: str) -> t.AuthSession | None:
        return await self._s.scalar(
            select(t.AuthSession).where(t.AuthSession.token_hash == token_hash)
        )

    async def touch(self, row: t.AuthSession, *, expires_at: datetime) -> None:
        """Corre el vencimiento hacia adelante: la expiración es por inactividad.

        Se escribe en cada request autenticada, que es un `UPDATE` por request.
        Es aceptable en SQLite con WAL y para el tamaño de esta app; si algún día
        molesta, la salida conocida es no tocar la fila cuando falta más de,
        digamos, un día para el vencimiento actual.
        """
        row.last_used_at = t.utcnow()
        row.expires_at = expires_at
        await self._s.flush()

    async def delete(self, row: t.AuthSession) -> None:
        await self._s.delete(row)
        await self._s.flush()

    async def delete_for_user(
        self, user_id: int, *, except_id: int | None = None
    ) -> int:
        """Cierra todas las sesiones del usuario. Con `except_id`, todas menos la
        actual: es lo que corresponde al cambiar la contraseña, porque quien la
        cambia no espera que lo eche también a él del dispositivo que está usando.
        """
        stmt = delete(t.AuthSession).where(t.AuthSession.user_id == user_id)
        if except_id is not None:
            stmt = stmt.where(t.AuthSession.id != except_id)
        result = await self._s.execute(stmt)
        await self._s.flush()
        return result.rowcount or 0

    async def purge_expired(self) -> int:
        """Borra las sesiones vencidas.

        No es seguridad —una sesión vencida ya no autentica, la comparación de
        `expires_at` se hace en cada request— sino higiene: sin esto la tabla
        crece con una fila por login hasta el fin de los tiempos.
        """
        result = await self._s.execute(
            delete(t.AuthSession).where(t.AuthSession.expires_at < t.utcnow())
        )
        await self._s.flush()
        return result.rowcount or 0
