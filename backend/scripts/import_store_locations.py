"""Escribe en esta base las ubicaciones que dejó `export_store_locations.py`.

Es la otra mitad del par, y la que se corre en el deploy. Una base recién creada
tiene el esquema pero ninguna sucursal —las migraciones crean tablas, no las
llenan, y una comparación inserta filas en `stores` sin coordenadas—, así que el
mapa contesta honestamente que no hay nada que dibujar hasta que alguien cargue
esto.

Inserta con el mismo `upsert` que usa el descubrimiento (`persist_locations`),
o sea que **es repetible**: correrlo dos veces deja la base igual que correrlo
una, y correrlo sobre una base que ya tiene ubicaciones las actualiza en lugar de
duplicarlas.

Las sucursales cargadas a mano son de una cuenta, así que hay que decidir de
cuál. Con una sola cuenta en la base no hace falta decirlo: se detecta sola. Con
más de una, el script se niega a adivinar y pide `--custom-for` explícito —
asignárselas a la cuenta equivocada sería peor que no importarlas.

Uso:  python scripts/import_store_locations.py
      python scripts/import_store_locations.py --custom-for vos@ejemplo.com   # con más de una cuenta
      python scripts/import_store_locations.py /ruta/al/archivo.json

En el contenedor, con el archivo ya dentro de la imagen:

      docker compose exec ahorrito python scripts/import_store_locations.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy.exc import IntegrityError

from app.core.db import dispose_engine, get_session_factory
from app.db.repositories import ChainRepository, CustomStoreRepository, UserRepository
from app.models.catalog import Chain as DomainChain
from app.services.locations import SOURCE_API, Location
from app.services.locations.persist import persist_locations

BACKEND_DIR = Path(__file__).resolve().parents[1]
DEFAULT_PATH = BACKEND_DIR / "data" / "store_locations.json"

SUPPORTED_VERSION = 1


def read_payload(path: Path) -> dict:
    if not path.exists():
        raise SystemExit(
            f"No existe {path}.\n"
            "Generalo con scripts/export_store_locations.py en la base que ya "
            "tiene las sucursales ubicadas."
        )
    payload = json.loads(path.read_text(encoding="utf-8"))
    version = payload.get("version")
    if version != SUPPORTED_VERSION:
        # Cortar acá y no intentar adivinar: importar la mitad de las sucursales
        # deja un mapa que muestra tres cadenas de cuatro sin decir que falta
        # una, que es peor que no importar nada.
        raise SystemExit(
            f"El archivo dice ser versión {version!r} y este script entiende la "
            f"{SUPPORTED_VERSION}. Volvé a exportarlo."
        )
    return payload


def to_locations(payload: dict) -> dict[str, list[Location]]:
    """Los registros del archivo, agrupados por cadena."""
    by_chain: dict[str, list[Location]] = {}
    for record in payload.get("stores", []):
        slug = record["chain_slug"]
        by_chain.setdefault(slug, []).append(
            Location(
                chain_slug=slug,
                external_id=record["external_id"],
                name=record["name"],
                latitude=record["latitude"],
                longitude=record["longitude"],
                # Si el registro no trae origen es de una exportación vieja,
                # anterior a que la columna existiera: se asume el de la cadena,
                # que es de dónde venía todo lo que había entonces.
                source=record.get("source") or SOURCE_API,
                address=record.get("address"),
                city=record.get("city"),
                province=record.get("province"),
                postal_code=record.get("postal_code"),
            )
        )
    return by_chain


async def resolve_owner(session, email: str | None):
    """A qué cuenta asignarle las sucursales a mano, sin adivinar de más.

    Con un solo dueño posible —el caso normal de esta app, con el registro
    cerrado en el deploy— pedir el email es puro trámite: solo hay una cuenta a
    la que puede ser. Con más de una ya no hay una lectura obviamente correcta,
    y equivocarse acá no es un error visible: la sucursal quedaría cargada, solo
    que en el mapa de otra persona. Por eso ahí sí hace falta decirlo.
    """
    users = UserRepository(session)
    if email is not None:
        normalized = email.strip().lower()
        user = await users.by_email(normalized)
        if user is None:
            raise SystemExit(
                f"No hay ninguna cuenta con el email {normalized}. Las "
                "sucursales a mano son de alguien: creá la cuenta primero, o "
                "usá --skip-custom para importar solo las de las cadenas."
            )
        return user

    accounts = await users.all()
    if not accounts:
        raise SystemExit(
            "Todavía no hay ninguna cuenta en esta base. Las sucursales a mano "
            "son de alguien: creá la cuenta primero con scripts/set_password.py "
            "--create, o usá --skip-custom para importar solo las de las "
            "cadenas."
        )
    if len(accounts) > 1:
        listado = "\n".join(f"    {u.email}" for u in accounts)
        raise SystemExit(
            "Hay más de una cuenta en esta base, así que no se puede elegir "
            f"sola:\n{listado}\n"
            "Decime cuál con --custom-for <email>, o usá --skip-custom para "
            "importar solo las de las cadenas."
        )
    return accounts[0]


async def import_custom(session, payload: dict, email: str | None) -> tuple[int, int]:
    """Las sucursales cargadas a mano, para la cuenta que corresponda.

    Devuelve cuántas se crearon y cuántas ya estaban. Se saltean las repetidas en
    vez de pisarlas: del otro lado puede haberlas corregido a mano después de
    exportar, y este script no tiene forma de saber cuál de las dos versiones es
    la buena. Para rehacer una, se borra desde «Mis sucursales» y se vuelve a
    importar.
    """
    records = payload.get("custom_stores")
    if not records:
        return (0, 0)

    user = await resolve_owner(session, email)
    print(f"  cuenta: {user.email}")

    repo = CustomStoreRepository(session, user.id)
    chains = ChainRepository(session)
    display_names = payload.get("chains") or {}
    existing = {(row.chain_id, row.name) for row in await repo.list()}

    created = skipped = 0
    for record in records:
        slug = record["chain_slug"]
        chain = await chains.by_slug(slug)
        if chain is None:
            # La cadena puede no existir todavía: `persist_locations` solo crea
            # las que traen ubicaciones propias, y Disco no publica ninguna. Es
            # justo la cadena que más necesita la carga a mano, así que se crea
            # acá con el mismo criterio —nombre del archivo, y la primera
            # comparación le pone lo suyo— en vez de perder la sucursal.
            chain = await chains.upsert(
                DomainChain(
                    slug=slug,
                    display_name=display_names.get(slug, slug),
                    supports_store_prices=False,
                )
            )
            await session.commit()
        if (chain.id, record["name"]) in existing:
            skipped += 1
            continue
        try:
            await repo.create(
                {
                    "chain_id": chain.id,
                    "name": record["name"],
                    "latitude": record["latitude"],
                    "longitude": record["longitude"],
                    "address": record.get("address"),
                    "city": record.get("city"),
                    "province": record.get("province"),
                    "postal_code": record.get("postal_code"),
                    "notes": record.get("notes"),
                }
            )
            await session.commit()
            created += 1
        except IntegrityError:
            await session.rollback()
            skipped += 1
    return (created, skipped)


async def main(path: Path, custom_for: str | None, skip_custom: bool) -> None:
    payload = read_payload(path)
    by_chain = to_locations(payload)
    display_names = payload.get("chains") or {}

    print(f"Importando desde {path}")
    print(f"  exportado el {payload.get('exported_at', '?')}\n")

    factory = get_session_factory()
    async with factory() as session:
        saved = await persist_locations(
            session, by_chain, display_names=display_names
        )
        for slug, locations in sorted(by_chain.items()):
            print(f"  {slug:<16} {len(locations):>4}")
        print(f"  {'TOTAL':<16} {saved:>4}")

        if payload.get("custom_stores") and not skip_custom:
            print("\nSucursales cargadas a mano:")
            created, skipped = await import_custom(session, payload, custom_for)
            print(f"  {created} creadas, {skipped} ya estaban o se saltearon")
        elif payload.get("custom_stores"):
            print(
                f"\n  Se salteó {len(payload['custom_stores'])} sucursales "
                "cargadas a mano (--skip-custom)."
            )
    await dispose_engine()

    print("\nListo. El mapa ya tiene qué dibujar.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "file",
        nargs="?",
        type=Path,
        default=DEFAULT_PATH,
        help=f"Archivo a importar. Por defecto {DEFAULT_PATH.relative_to(BACKEND_DIR)}.",
    )
    parser.add_argument(
        "--custom-for",
        metavar="EMAIL",
        help=(
            "Cuenta a la que asignarle las sucursales cargadas a mano que traiga "
            "el archivo. Con una sola cuenta en la base no hace falta: se "
            "detecta sola. Con más de una, es obligatorio."
        ),
    )
    parser.add_argument(
        "--skip-custom",
        action="store_true",
        help="No importar las sucursales cargadas a mano, aunque el archivo las traiga.",
    )
    args = parser.parse_args()
    asyncio.run(main(args.file, args.custom_for, args.skip_custom))
