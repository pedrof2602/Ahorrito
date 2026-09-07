"""Carga las ubicaciones de las sucursales en la base.

Se corre a mano, no en el arranque de la app: son ~1.250 ubicaciones y las de
Coto hay que geocodificarlas de a una por segundo, que es la política de uso de
Nominatim. Es un trabajo de minutos que se hace una vez y se repite cuando abren
o cierran sucursales.

Informa dos números distintos que conviene no confundir: cuántas sucursales
*encontró* y cuántas pudo *ubicar*. Una sucursal sin coordenada no es un error
—Disco no publica ninguna—, simplemente no se dibuja en el mapa.

Uso:  ./venv/bin/python scripts/refresh_store_locations.py
      ./venv/bin/python scripts/refresh_store_locations.py --skip-geocoding
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from dataclasses import dataclass, replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings
from app.core.db import dispose_engine, get_session_factory
from app.db.repositories import ChainRepository, StoreRepository
from app.models.catalog import Chain
from app.services.locations import Location, coto_scrape, vtex_pickup
from app.services.locations.georef import GeorefGeocoder
from app.services.locations.nominatim import Geocoder
from app.services.providers.vtex.stores import VTEX_STORES


async def collect_vtex() -> dict[str, list[Location]]:
    """Sucursales de las cadenas VTEX. Ya vienen con coordenadas."""
    out: dict[str, list[Location]] = {}
    for config in VTEX_STORES:
        print(f"  {config.display_name}: consultando puntos de retiro…", flush=True)
        found = await vtex_pickup.fetch_locations(
            config.slug,
            config.base_url,
            user_agent=settings.HTTP_USER_AGENT,
            timeout_s=settings.HTTP_TIMEOUT_S,
            min_interval_ms=settings.HTTP_MIN_INTERVAL_MS,
        )
        out[config.slug] = found
        print(f"     {len(found)} ubicaciones", flush=True)
    return out


CABA = (-34.71, -34.52, -58.54, -58.33)
"""Caja de CABA (lat mín, lat máx, lon mín, lon máx), holgada."""

AMBA = (-35.10, -34.30, -59.05, -58.15)
"""CABA más el conurbano, que es lo que Coto llama «Zona Norte/Sur/Oeste»."""


@dataclass(frozen=True, slots=True)
class Zone:
    """Dónde queda una zona de las que publica Coto.

    `province` acota la consulta y `box` valida la respuesta; hacen falta las
    dos. Pedir «Av. Belgrano 950» en provincia de Buenos Aires devuelve Tres
    Arroyos, que efectivamente está en provincia de Buenos Aires y a quinientos
    kilómetros del Coto de Garín.

    `skip` marca las zonas donde ni con las dos alcanza y por eso no se
    geocodifican. Ver `ZONES`.
    """

    province: str = ""
    box: tuple[float, float, float, float] | None = None
    skip: bool = False


ZONES = {
    "capital federal": Zone("Ciudad Autónoma de Buenos Aires", CABA),
    "santa fe": Zone("Santa Fe"),
    "entre rios": Zone("Entre Ríos"),
    "neuquén": Zone("Neuquén"),
    "mendoza": Zone("Mendoza"),
    "mar del plata": Zone("Buenos Aires", (-38.15, -37.85, -57.70, -57.45)),
    "partido de la costa": Zone("Buenos Aires", (-36.90, -36.20, -57.00, -56.60)),
    # --- las que no se geocodifican ---------------------------------------
    "zona norte": Zone(skip=True),
    "zona sur": Zone(skip=True),
    "zona oeste": Zone(skip=True),
}
"""Las zonas que publica Coto, traducidas a geografía.

Son categorías comerciales, no unidades administrativas: ningún callejero
conoce «Zona Oeste». Sin traducirlas la consulta sale sin filtro y el resultado
es malo de la peor manera —«Av. San Martín 2095» de una sucursal porteña sale
resuelto en Corrientes, con total confianza— porque una coordenada equivocada se
ve exactamente igual que una correcta.

**El conurbano no se geocodifica.** Se intentó y se midió: acotando por
provincia y validando contra una caja del AMBA entran 22 sucursales, y al
verificarlas por geocodificación inversa **ocho caen en el partido equivocado**
—Munro en Berazategui, Castelar en Vicente López, José C. Paz en San Fernando—.
El problema no tiene arreglo desde acá: Coto publica «Zona Norte» y una calle
que se repite en veinte partidos, y no hay filtro que invente el dato que falta.
CABA sí se resuelve bien (verificado: las 58 caen en su barrio) porque su
callejero no tiene esa ambigüedad.

Para el conurbano está la carga manual desde «Mis sucursales», que además le
gana a la automática justamente por esto.
"""


CITY_IN_ADDRESS = re.compile(r"\s*\(([^)]+)\)\s*$")
"""Coto mete la ciudad dentro del texto de la dirección: «Urquiza 1644
(Rosario)». Sacarla de ahí no es cosmética — sin ese dato la dirección se
resuelve a doscientos kilómetros de Rosario, dentro de la misma provincia."""


def geocoding_hint(store: Location) -> tuple[Location, Zone | None]:
    """La consulta acotada por zona, y con qué validar lo que vuelva."""
    address, city = store.address or "", None
    found = CITY_IN_ADDRESS.search(address)
    if found:
        address, city = address[: found.start()].strip(), found.group(1).strip()

    zone = ZONES.get((store.city or "").strip().lower())
    if zone is None:
        return replace(store, address=address, city=city or store.city), None
    # La zona de Coto reemplaza a la ciudad salvo que la dirección traiga una
    # más precisa: «Zona Norte» no es un partido, «Rosario» sí.
    return replace(store, address=address, city=city, province=zone.province), zone


def plausible(located: Location, zone: Zone | None) -> bool:
    """¿La coordenada cae donde Coto dice que está la sucursal?

    Es el filtro que decide qué se guarda. Sin él, de 122 sucursales entran 98
    y **cuarenta están mal**: «Coto Fisherton (Rosario)» aterriza en Mendoza y
    «Coto Santa Fe» en Bahía Blanca. Un marker en la ciudad equivocada es peor
    que ningún marker —el mapa afirma algo falso con la misma cara con la que
    afirma lo verdadero— y para las que quedan afuera está la carga manual, que
    además es más precisa que cualquier heurística que se escriba acá.
    """
    if zone is None or zone.box is None:
        return True
    lat_min, lat_max, lon_min, lon_max = zone.box
    return (
        lat_min <= located.latitude <= lat_max
        and lon_min <= located.longitude <= lon_max
    )


async def collect_coto(session, skip_geocoding: bool, geocoder_name: str) -> list[Location]:
    """Sucursales de Coto. Vienen sin coordenadas: hay que geocodificarlas."""
    print("  Coto: descargando la tabla de sucursales…", flush=True)
    scraped = await coto_scrape.fetch_stores(user_agent=settings.HTTP_USER_AGENT)
    print(f"     {len(scraped)} sucursales en la tabla", flush=True)

    if skip_geocoding:
        print("     geocodificación salteada (--skip-geocoding)", flush=True)
        return []

    if geocoder_name == "georef":
        maker = lambda: GeorefGeocoder(session=session)  # noqa: E731
        print(f"     geocodificando con Georef (~{len(scraped) // 5}s)…", flush=True)
    else:
        maker = lambda: Geocoder(session)  # noqa: E731
        print(
            f"     geocodificando con Nominatim "
            f"(1 por segundo, ~{len(scraped)}s la primera vez)…",
            flush=True,
        )

    located: list[Location] = []
    descartadas = 0
    salteadas = 0
    async with maker() as geocoder:
        for index, store in enumerate(scraped, 1):
            hint, zone = geocoding_hint(store)
            if zone is not None and zone.skip:
                salteadas += 1
                continue
            resolved = await geocoder.locate(hint)
            if resolved is not None:
                if plausible(resolved, zone):
                    # Se guarda el texto original y no el que se usó para
                    # consultar: lo que el popup tiene que mostrar es lo que
                    # publica Coto —«Urquiza 1644 (Rosario)», «Capital
                    # Federal»—, no la versión recortada para el callejero.
                    located.append(
                        replace(resolved, address=store.address, city=store.city)
                    )
                else:
                    descartadas += 1
            if index % 25 == 0:
                print(f"     {index}/{len(scraped)}…", flush=True)
            # La caché es por sesión: se commitea a medida que avanza para que
            # un Ctrl+C a mitad de camino no tire las direcciones ya resueltas.
            await session.commit()

    print(f"     {len(located)} ubicadas de {len(scraped)}", flush=True)
    if descartadas:
        print(f"     {descartadas} descartadas por caer fuera de su zona", flush=True)
    if salteadas:
        print(
            f"     {salteadas} del conurbano no se geocodifican: Coto publica "
            "«Zona Norte/Sur/Oeste»\n"
            "       y con eso el geocodificador acierta el partido poco más de "
            "la mitad de las veces.",
            flush=True,
        )
    if descartadas or salteadas:
        print("     Esas se cargan a mano desde «Mis sucursales».", flush=True)
    return located


async def persist(session, by_chain: dict[str, list[Location]]) -> None:
    chains = ChainRepository(session)
    stores = StoreRepository(session)

    for slug, locations in by_chain.items():
        if not locations:
            continue
        row = await chains.by_slug(slug)
        if row is None:
            # La cadena puede no estar todavía si nunca se comparó nada. Se crea
            # con el nombre del slug; la primera comparación le pone el suyo.
            row = await chains.upsert(
                Chain(slug=slug, display_name=slug, supports_store_prices=False)
            )
        for location in locations:
            await stores.upsert_location(row.id, location)
        await session.commit()


async def main(skip_geocoding: bool, geocoder_name: str) -> None:
    factory = get_session_factory()
    async with factory() as session:
        print("Descubriendo sucursales:")
        by_chain = await collect_vtex()
        by_chain["coto-ar"] = await collect_coto(
            session, skip_geocoding, geocoder_name
        )

        print("\nGuardando…")
        await persist(session, by_chain)

        print("\nResumen:")
        total = 0
        for slug, locations in sorted(by_chain.items()):
            print(f"  {slug:<16} {len(locations):>4} ubicadas")
            total += len(locations)
        print(f"  {'TOTAL':<16} {total:>4}")
        if not by_chain.get("disco-ar"):
            print(
                "\n  Disco no publica sus sucursales (pickup-points devuelve 0). "
                "No es un fallo: no va a aparecer en el mapa."
            )
    await dispose_engine()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--skip-geocoding",
        action="store_true",
        help="Solo las cadenas que publican coordenadas. Sin geocodificar nada.",
    )
    parser.add_argument(
        "--geocoder",
        choices=("georef", "nominatim"),
        default="georef",
        help=(
            "Con qué geocodificar las direcciones de Coto. Por defecto Georef, "
            "la API del Estado argentino: llega desde redes que tienen "
            "bloqueado Nominatim y acierta más en direcciones de acá."
        ),
    )
    args = parser.parse_args()
    asyncio.run(main(args.skip_geocoding, args.geocoder))
