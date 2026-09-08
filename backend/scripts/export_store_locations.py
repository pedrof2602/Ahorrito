"""Saca a un archivo las sucursales ya ubicadas de esta base.

Existe porque ubicar sucursales cuesta y el resultado no es reproducible.
`refresh_store_locations.py` tarda minutos —las de Coto se geocodifican de a una
y después se descarta la mitad por caer fuera de su zona— y depende de que dos
servicios de afuera contesten hoy lo mismo que ayer. Repetir ese trabajo en cada
base (la de desarrollo, la del deploy, la del volumen nuevo después de un
incidente) es pagarlo de nuevo y, peor, arriesgarse a un resultado distinto: si
mañana Georef resuelve una dirección peor, la base nueva queda peor que la vieja
sin que nadie lo haya decidido. Exportar convierte esa corrida en un dato
versionado que se puede revisar en un diff.

El archivo por defecto va a `data/`, que es la carpeta que viaja dentro de la
imagen de Docker. Eso es a propósito: es lo que permite llevar las ubicaciones
al deploy sin subir nada por separado —se exporta, se commitea, y el próximo
deploy ya las lleva adentro para que `import_store_locations.py` las escriba en
el volumen—.

**No exporta precios ni cuentas.** Solo dónde queda cada local, que es
información pública de las cadenas. Las sucursales cargadas a mano son la
excepción y por eso están detrás de `--include-custom`: esas sí las escribió una
persona, van sin dueño en el archivo, y el importador exige a quién asignárselas.

Uso:  ./venv/bin/python scripts/export_store_locations.py
      ./venv/bin/python scripts/export_store_locations.py --include-custom
      ./venv/bin/python scripts/export_store_locations.py -o /tmp/sucursales.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select

from app.core.db import dispose_engine, get_session_factory
from app.db.tables import Chain, CustomStore, Store

BACKEND_DIR = Path(__file__).resolve().parents[1]
DEFAULT_PATH = BACKEND_DIR / "data" / "store_locations.json"

FORMAT_VERSION = 1
"""Versión del formato del archivo, que el importador verifica.

Sirve para que un archivo viejo contra un importador nuevo falle diciéndolo, en
vez de escribir a medias: media base de sucursales es exactamente el estado en
el que el mapa miente sin avisar.
"""


def _clean(**fields: object) -> dict[str, object]:
    """Un registro sin las claves que no aportan nada.

    Las columnas vacías se sacan en vez de guardarse en `null`: son mayoría
    —muchas sucursales no publican provincia ni CP— y dejarlas engorda el
    archivo y ensucia el diff sin decir nada que la ausencia no diga igual.
    """
    return {key: value for key, value in fields.items() if value is not None}


async def collect(session, include_custom: bool) -> dict[str, object]:
    chains = {chain.id: chain for chain in await session.scalars(select(Chain))}

    located = await session.scalars(
        select(Store)
        .where(Store.latitude.is_not(None), Store.longitude.is_not(None))
        .order_by(Store.chain_id, Store.external_id)
    )
    stores = []
    for row in located:
        chain = chains.get(row.chain_id)
        if chain is None:
            # Una sucursal cuya cadena no está es una fila huérfana, no un dato:
            # exportarla haría fallar la importación por una FK que no existe.
            continue
        stores.append(
            _clean(
                chain_slug=chain.slug,
                external_id=row.external_id,
                name=row.name,
                latitude=row.latitude,
                longitude=row.longitude,
                source=row.location_source,
                address=row.address,
                city=row.city,
                province=row.province,
                postal_code=row.location_postal_code,
            )
        )

    payload: dict[str, object] = {
        "version": FORMAT_VERSION,
        "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        # El nombre lindo viaja para que una base recién creada muestre «Coto» y
        # no «coto-ar» en el popup del marker. Solo se usa al crear la cadena.
        "chains": {
            chain.slug: chain.display_name
            for chain in sorted(chains.values(), key=lambda c: c.slug)
        },
        "stores": stores,
    }

    if include_custom:
        rows = await session.scalars(
            select(CustomStore).order_by(CustomStore.chain_id, CustomStore.name)
        )
        customs = []
        for row in rows:
            chain = chains.get(row.chain_id)
            if chain is None:
                continue
            customs.append(
                _clean(
                    chain_slug=chain.slug,
                    name=row.name,
                    latitude=row.latitude,
                    longitude=row.longitude,
                    address=row.address,
                    city=row.city,
                    province=row.province,
                    postal_code=row.postal_code,
                    notes=row.notes,
                )
            )
        # `user_id` no se exporta a propósito: los ids de cuenta no significan lo
        # mismo en dos bases distintas, y copiarlo sería asignarle las sucursales
        # a quien haya quedado con ese número del otro lado.
        payload["custom_stores"] = customs

    return payload


def write(payload: dict[str, object], path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Un registro por línea: con ~1.400 sucursales, el diff de la próxima
    # exportación tiene que poder leerse («cambiaron 6») en vez de ser una línea
    # de 300 kB que git marca entera como modificada.
    text = json.dumps(payload, ensure_ascii=False, indent=1) + "\n"
    path.write_text(text, encoding="utf-8")
    return len(text.encode("utf-8"))


async def main(path: Path, include_custom: bool) -> None:
    factory = get_session_factory()
    async with factory() as session:
        payload = await collect(session, include_custom)
    await dispose_engine()

    size = write(payload, path)

    stores: list[dict] = payload["stores"]  # type: ignore[assignment]
    by_chain: dict[str, int] = {}
    for store in stores:
        by_chain[store["chain_slug"]] = by_chain.get(store["chain_slug"], 0) + 1

    print(f"{path}  ({size / 1024:.0f} kB)\n")
    for slug, count in sorted(by_chain.items()):
        print(f"  {slug:<16} {count:>4}")
    customs = payload.get("custom_stores")
    if customs is not None:
        print(f"  {'a mano':<16} {len(customs):>4}")
    print(f"  {'TOTAL':<16} {len(stores) + len(customs or []):>4}")

    if not stores:
        print(
            "\n  La base no tiene ninguna sucursal ubicada: esto exportó un "
            "archivo vacío.\n  Corré antes scripts/refresh_store_locations.py."
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=DEFAULT_PATH,
        help=f"Adónde escribir. Por defecto {DEFAULT_PATH.relative_to(BACKEND_DIR)}.",
    )
    parser.add_argument(
        "--include-custom",
        action="store_true",
        help=(
            "Incluir también las sucursales cargadas a mano, sin su dueño. Al "
            "importarlas hay que decir de quién son."
        ),
    )
    args = parser.parse_args()
    asyncio.run(main(args.output, args.include_custom))
