"""Qué sucursal de cada cadena le queda más cerca al usuario.

Es lo que consume el mapa. Dos pasos: ubicar al usuario, y para cada cadena
elegir la sucursal ubicada más próxima.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories import (
    AddressRepository,
    CustomStoreRepository,
    StoreRepository,
)
from app.db.tables import Chain, CustomStore, Store

from . import SOURCE_MANUAL, haversine_km

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class NearestStore:
    chain_slug: str
    external_id: str
    name: str
    latitude: float
    longitude: float
    distance_km: float | None
    location_source: str | None
    address: str | None
    city: str | None
    custom_store_id: int | None = None
    """El id en `custom_stores` si esta sucursal la cargó el usuario.

    Lo necesita el frontend para poder ofrecer «editar» o «borrar» sobre el
    marker: sin el id, la única forma de corregir una sucursal mal ubicada sería
    ir a buscarla a otra pantalla.
    """


@dataclass(frozen=True, slots=True)
class Center:
    """Desde dónde se miden las distancias, y por qué desde ahí."""

    latitude: float
    longitude: float
    source: str
    """`explicit`, `address`, `postal_code` o `geocoded`."""
    label: str | None = None
    """Qué se usó, para poder decirlo en pantalla: el nombre del domicilio."""

    @property
    def point(self) -> tuple[float, float]:
        return (self.latitude, self.longitude)


async def resolve_center(
    session: AsyncSession,
    postal_code: str,
    *,
    user_id: int,
    explicit: tuple[float, float] | None = None,
    geocoder=None,
) -> Center | None:
    """Dónde está parado el usuario, de lo más preciso a lo más aproximado.

    1. Las coordenadas que vinieron en el pedido, si vinieron. Es alguien
       diciendo explícitamente «medí desde acá» y no hay nada que refinar.
    2. El domicilio predeterminado, si está ubicado. Es la dirección exacta, con
       lo que «la sucursal más cercana» pasa a significar cerca de tu casa y no
       cerca del centro de tu código postal.
    3. El promedio de las sucursales que ya están en ese código postal. Es
       aproximado —un CP puede abarcar varias decenas de cuadras— pero sale de
       la base, es instantáneo y no puede fallar.
    4. Geocodificar el código postal, que es el camino lento y el único que
       depende de que haya red.

    El orden importa más de lo que parece: un CP de CABA cubre un área donde
    entran tres sucursales de la misma cadena, así que sin el paso 2 «la más
    cercana» puede ser una que te queda a quince cuadras de la que tenés al lado.
    """
    if explicit is not None:
        return Center(explicit[0], explicit[1], source="explicit")

    home = await AddressRepository(session, user_id).default_located()
    if home is not None:
        return Center(
            home.latitude,
            home.longitude,
            source="address",
            label=home.label,
        )

    stores = await StoreRepository(session).located_in_postal_code(postal_code)
    if stores:
        return Center(
            sum(s.latitude for s in stores) / len(stores),
            sum(s.longitude for s in stores) / len(stores),
            source="postal_code",
        )

    if geocoder is None:
        return None
    coords = await geocoder.resolve(f"{postal_code}, Argentina")
    if coords is None:
        return None
    return Center(coords[0], coords[1], source="geocoded")


async def nearest_by_chain(
    session: AsyncSession,
    center: tuple[float, float] | None,
    *,
    user_id: int,
) -> list[NearestStore]:
    """La sucursal ubicada más cercana de cada cadena.

    Sin centro se devuelve una sucursal por cadena igual, con `distance_km` en
    null: el mapa puede dibujar markers sin saber cuál te queda más cerca, y
    devolver una lista vacía sería esconder ubicaciones que sí tenemos.

    Compiten las sucursales que publicaron las cadenas y las que cargó el
    usuario. **Entre las dos, la manual gana siempre**, aunque quede más lejos:
    quien la cargó lo hizo porque la automática está mal ubicada o no existe, y
    que la automática le gane por doscientos metros calculados sobre una
    coordenada que ya sabemos dudosa deshace el trabajo que esa persona hizo.
    """
    rows = await StoreRepository(session).located()
    custom = await CustomStoreRepository(session, user_id).list()
    if not rows and not custom:
        return []

    # El slug se resuelve en una consulta y no una por sucursal: son ~1.250 filas
    # y `row.chain.slug` dispararía un lazy load por cada una, que con
    # `AsyncSession` además es `MissingGreenlet`.
    chains = {chain.id: chain.slug for chain in await session.scalars(select(Chain))}

    def distance_to(latitude: float, longitude: float) -> float | None:
        if center is None:
            return None
        return haversine_km(center[0], center[1], latitude, longitude)

    # (es_manual, distancia): el orden de la tupla es la regla de desempate. El
    # bool va primero para que ninguna distancia alcance a dar vuelta la
    # preferencia por lo cargado a mano.
    best: dict[str, tuple[tuple[bool, float], float | None, Store | CustomStore]] = {}

    def offer(slug: str, row: Store | CustomStore, manual: bool) -> None:
        distance = distance_to(row.latitude, row.longitude)
        # Sin centro no hay con qué comparar y gana el primero que llega, salvo
        # que aparezca uno manual: ahí sí hay criterio.
        rank = (not manual, distance if distance is not None else float("inf"))
        current = best.get(slug)
        if current is None or rank < current[0]:
            best[slug] = (rank, distance, row)

    for row in rows:
        slug = chains.get(row.chain_id)
        if slug is not None:
            offer(slug, row, manual=False)

    for row in custom:
        slug = chains.get(row.chain_id)
        if slug is not None:
            offer(slug, row, manual=True)

    return [
        NearestStore(
            chain_slug=slug,
            # Una sucursal manual no tiene `external_id` en el catálogo de la
            # cadena, y ese es justamente el motivo por el que no puede resolver
            # precios. El prefijo lo deja explícito en vez de inventar un id que
            # parezca del proveedor.
            external_id=(
                row.external_id
                if isinstance(row, Store)
                else f"{SOURCE_MANUAL}:{row.id}"
            ),
            name=row.name,
            latitude=row.latitude,
            longitude=row.longitude,
            distance_km=round(distance, 2) if distance is not None else None,
            location_source=(
                row.location_source if isinstance(row, Store) else SOURCE_MANUAL
            ),
            address=row.address,
            city=row.city,
            custom_store_id=None if isinstance(row, Store) else row.id,
        )
        for slug, (_rank, distance, row) in sorted(
            best.items(), key=lambda kv: (kv[1][1] is None, kv[1][1] or 0)
        )
    ]
