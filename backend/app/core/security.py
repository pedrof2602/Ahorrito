"""Hash de contraseñas y tokens de sesión.

Las dos mitades usan primitivas distintas a propósito, y la diferencia es el
punto entero de este módulo:

- La **contraseña** la elige una persona, así que tiene poca entropía y se puede
  atacar por diccionario. Va con Argon2id, que está diseñado para ser lento y
  caro en memoria.
- El **token de sesión** lo genera `secrets`, así que ya tiene 256 bits de
  entropía y no hay diccionario que probar. Va con SHA-256 pelado, que es rápido
  porque se ejecuta en cada request de la app.

Usar Argon2 para el token agregaría ~50 ms a cada request para defenderse de un
ataque que no existe; usar SHA-256 para la contraseña dejaría el archivo de
hashes crackeable a millones de intentos por segundo.
"""

from __future__ import annotations

import hashlib
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

_hasher = PasswordHasher()
"""Parámetros por default de argon2-cffi, que siguen la recomendación RFC 9106
de bajo consumo de memoria (64 MiB, 3 pasadas). Subirlos es legítimo, pero el
costo lo paga el login de cada usuario y esta app corre en un server chico."""

UNUSABLE_PASSWORD = "!"
"""Centinela para una cuenta que todavía no tiene contraseña.

No es un hash de Argon2 válido, así que `verify_password` sale por
`InvalidHashError` y devuelve False. Lo importante es que nunca es igual a nada
que alguien pueda tipear: dejar el campo en `""` haría que una contraseña vacía
compare bien contra un hash vacío si algún día se colara una comparación por
igualdad."""


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """Contrasta la contraseña contra el hash, sin filtrar el porqué del fallo.

    Devuelve un bool y no distingue "hash inválido" de "contraseña equivocada":
    quien llama no tiene nada distinto que hacer en cada caso, y propagar la
    diferencia hasta la respuesta HTTP le diría al atacante si la cuenta existe.
    """
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    """Si el hash quedó con parámetros más flojos que los actuales.

    Argon2 versiona sus parámetros dentro del propio hash, así que subir el costo
    en `_hasher` no invalida lo ya guardado. Se aprovecha el login —el único
    momento en que la contraseña en claro está a mano— para regrabarlo.
    """
    try:
        return _hasher.check_needs_rehash(password_hash)
    except InvalidHashError:
        return False


def new_session_token() -> str:
    """El token que viaja en la cookie. 32 bytes = 256 bits de entropía."""
    return secrets.token_urlsafe(32)


def hash_session_token(token: str) -> str:
    """Lo que se guarda en `auth_sessions.token_hash`."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def normalize_email(email: str) -> str:
    """Minúsculas y sin espacios en los bordes.

    Se aplica en el registro y en el login por igual. Si viviera solo en el
    login, `Pedro@Gmail.com` y `pedro@gmail.com` serían dos cuentas distintas
    para el índice único y la misma para quien intenta entrar.
    """
    return email.strip().lower()
