"""Asigna o cambia la contraseña de una cuenta desde la terminal.

Existe por un caso concreto: la migración que introdujo el login creó tu cuenta
—la que es dueña de todo lo que cargaste antes— sin contraseña usable, porque la
alternativa era dejar una contraseña por default escrita en un archivo del repo.
Este script es cómo le ponés la primera.

    ./venv/bin/python scripts/set_password.py                   # lista las cuentas
    ./venv/bin/python scripts/set_password.py dueno@localhost   # cambia esa
    ./venv/bin/python scripts/set_password.py dueno@localhost --email nuevo@mail.com

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


async def _set_password(email: str, new_email: str | None) -> int:
    async with get_session_factory()() as session:
        users = UserRepository(session)
        user = await users.by_email(normalize_email(email))
        if user is None:
            print(f"No existe ninguna cuenta con el email {email!r}.", file=sys.stderr)
            print("Corré el script sin argumentos para ver las que hay.", file=sys.stderr)
            return 1

        password = getpass.getpass(f"Contraseña nueva para {user.email}: ")
        if len(password) < settings.PASSWORD_MIN_LENGTH:
            print(
                f"Necesita al menos {settings.PASSWORD_MIN_LENGTH} caracteres.",
                file=sys.stderr,
            )
            return 1
        if password != getpass.getpass("Repetila: "):
            print("No coinciden.", file=sys.stderr)
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
    args = parser.parse_args()

    try:
        if args.email is None:
            return await _list_accounts()
        return await _set_password(args.email, args.new_email)
    finally:
        await dispose_engine()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
