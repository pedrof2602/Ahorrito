"""Dejar escritas en la base las ubicaciones que se descubrieron.

Vive acá y no dentro del script que las sale a buscar porque hay dos caminos que
terminan en la misma escritura: `refresh_store_locations.py`, que las descubre
consultando a las cadenas, e `import_store_locations.py`, que las trae de un
archivo ya exportado. Las dos puntas tienen que insertar exactamente igual
—incluido el caso de la cadena que todavía no existe en la base—, y si eso
estuviera duplicado, la base importada podría terminar distinta de la
descubierta sin que nadie lo haya decidido.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories import ChainRepository, StoreRepository
from app.models.catalog import Chain

from . import Location


async def persist_locations(
    session: AsyncSession,
    by_chain: dict[str, list[Location]],
    *,
    display_names: dict[str, str] | None = None,
) -> int:
    """Guarda las ubicaciones y devuelve cuántas escribió.

    `display_names` es el nombre lindo de cada cadena, y solo se usa cuando hay
    que crearla: en una base donde ya se comparó algo, el nombre que vale es el
    que puso el proveedor, y pisarlo desde acá sería que importar ubicaciones
    —un dato del mapa— cambie cómo se llama la cadena en la tabla de precios.
    """
    chains = ChainRepository(session)
    stores = StoreRepository(session)
    names = display_names or {}

    saved = 0
    for slug, locations in by_chain.items():
        if not locations:
            continue
        row = await chains.by_slug(slug)
        if row is None:
            # La cadena puede no estar todavía si nunca se comparó nada. Se crea
            # con el nombre que venga —o con el slug, si no vino ninguno—; la
            # primera comparación le pone el suyo.
            row = await chains.upsert(
                Chain(
                    slug=slug,
                    display_name=names.get(slug, slug),
                    supports_store_prices=False,
                )
            )
        for location in locations:
            await stores.upsert_location(row.id, location)
            saved += 1
        await session.commit()
    return saved
