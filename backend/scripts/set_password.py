"""Asigna o cambia la contraseña de una cuenta desde la terminal.

Existe por dos casos, uno para cada tipo de base:

* **Base con datos de antes del login.** La migración que lo introdujo sembró
  tu cuenta con id 1 sin contraseña usable, porque la alternativa era dejar una
  contraseña por default escrita en un archivo del repo. Este script es cómo le
  ponés la primera.
* **Base recién creada** (un deploy nuevo, con el volumen vacío). Ahí la
  migración no siembra ninguna cuenta —no hay nada que rescatar—, y con
  `REGISTRATION_OPEN=false` tampoco se puede pasar por `/auth/register`. Acá el
  script crea la cuenta desde cero con `--create`.

    ./venv/bin/python scripts/set_password.py                       # lista las cuentas
    ./venv/bin/python scripts/set_password.py dueno@localhost       # cambia esa
    ./venv/bin/python scripts/set_password.py dueno@localhost --email nuevo@mail.com
    ./venv/bin/python scripts/set_password.py tu@mail.com --create  # primera cuenta en una base vacía

La contraseña se pide por `getpass`, nunca por argumento: lo que se escribe en
la línea de comandos queda en el historial del shell y en la lista de procesos,
donde lo lee cualquiera con acceso a la máquina.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select

from app.core.config import settings
from app.core.db import dispose_engine, get_session_factory
from app.core.security import UNUSABLE_PASSWORD, hash_password, normalize_email
from app.db import tables as t
from app.db.repositories import AuthSessionRepository, UserRepository


async def _list_accounts() -> int:
    async with get_session_factory()() as session:
        rows = list(await session.scalars(select(t.User).order_by(t.User.id)))

    if not rows:
        print("No hay cuentas todavía. Registrate desde la app.")
        return 0

    print(f"{'id':>4}  {'email':<40} {'rol':<8} contraseña")
    for row in rows:
        estado = "sin asignar" if row.password_hash == UNUSABLE_PASSWORD else "asignada"
        print(f"{row.id:>4}  {row.email:<40} {row.role:<8} {estado}")
    return 0


def _read_new_password(prompt_for: str) -> str | None:
    """Pide la contraseña dos veces y la valida. `None` si hay que abortar."""
    password = getpass.getpass(f"Contraseña nueva para {prompt_for}: ")
    if len(password) < settings.PASSWORD_MIN_LENGTH:
        print(
            f"Necesita al menos {settings.PASSWORD_MIN_LENGTH} caracteres.",
            file=sys.stderr,
        )
        return None
    if password != getpass.getpass("Repetila: "):
        print("No coinciden.", file=sys.stderr)
        return None
    return password


async def _create_account(email: str) -> int:
    """Crea una cuenta desde cero. Para una base sin ninguna todavía.

    Sin esto, una base recién creada —un deploy nuevo, sin datos previos que la
    migración pueda adoptar— no tiene forma de arrancar: no hay cuenta que
    `_set_password` pueda modificar, y `REGISTRATION_OPEN=false` bloquea
    `/auth/register`. Este es el único camino que no depende de ninguna de las
    dos.
    """
    email = normalize_email(email)
    async with get_session_factory()() as session:
        users = UserRepository(session)
        if await users.by_email(email) is not None:
            print(
                f"Ya existe una cuenta con el email {email!r}. Corré el script "
                "con ese email, sin --create, para cambiarle la contraseña.",
                file=sys.stderr,
            )
            return 1

        password = _read_new_password(email)
        if password is None:
            return 1

        user = await users.create(email, hash_password(password))
        await session.commit()

    print(f"Listo. Cuenta creada para {user.email}.")
    return 0


async def _set_password(email: str, new_email: str | None) -> int:
    async with get_session_factory()() as session:
        users = UserRepository(session)
        user = await users.by_email(normalize_email(email))
        if user is None:
            print(f"No existe ninguna cuenta con el email {email!r}.", file=sys.stderr)
            print(
                "Corré el script sin argumentos para ver las que hay, o con "
                "--create si la base todavía no tiene ninguna.",
                file=sys.stderr,
            )
            return 1

        password = _read_new_password(user.email)
        if password is None:
            return 1

        user.password_hash = hash_password(password)
        if new_email:
            user.email = normalize_email(new_email)

        # Cambiar la contraseña desde acá cierra todas las sesiones abiertas, por
        # el mismo motivo que el endpoint de la app: si el cambio es porque
        # alguien entró, dejarle la sesión viva lo vuelve inútil. Sin excepción
        # de "la sesión actual" porque la terminal no tiene ninguna.
        cerradas = await AuthSessionRepository(session).delete_for_user(user.id)
        await session.commit()

    print(f"Listo. Contraseña actualizada para {user.email}.")
    if cerradas:
        print(f"Se cerraron {cerradas} sesión/es abiertas.")
    return 0


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "email", nargs="?", help="Cuenta a modificar. Sin esto, lista las cuentas."
    )
    parser.add_argument(
        "--email", dest="new_email", help="Cambiar también el email de la cuenta."
    )
    parser.add_argument(
        "--create",
        action="store_true",
        help="Crear la cuenta si no existe, en vez de exigir que ya exista.",
    )
    args = parser.parse_args()

    if args.create and args.new_email:
        print("--create y --email no van juntos: la cuenta nueva ya nace con el "
              "email que le pasaste como argumento.", file=sys.stderr)
        return 1

    try:
        if args.email is None:
            return await _list_accounts()
        if args.create:
            return await _create_account(args.email)
        return await _set_password(args.email, args.new_email)
    finally:
        await dispose_engine()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
