"""Cifrado simétrico para los secretos de terceros que hay que poder *usar*.

Es la excepción a la regla del proyecto, y conviene entender por qué. Las
contraseñas se guardan hasheadas con Argon2 y los tokens de sesión con SHA-256:
de esos nunca hace falta recuperar el original, solo comparar contra lo que
alguien presenta. Un token de Amazon es lo contrario —hay que mandarlo tal cual
en el header de cada llamada a la API de listas—, así que la operación tiene que
ser reversible y lo único que queda es cifrarlo.

Fernet (AES-128-CBC + HMAC-SHA256, de `cryptography`) y no algo armado a mano:
trae el IV aleatorio, la autenticación y el formato de serialización resueltos, y
la forma de usarlo mal —reusar un nonce, olvidarse del MAC— no está expuesta.

Con `TOKEN_ENCRYPTION_KEY` vacía esto no funciona y no debe funcionar: preferimos
que la feature esté visiblemente apagada antes que guardar tokens en claro
porque faltaba una variable de entorno.
"""

from __future__ import annotations

from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings


class EncryptionUnavailable(RuntimeError):
    """No hay `TOKEN_ENCRYPTION_KEY`, o la que hay no es una clave Fernet.

    Se levanta al cifrar o descifrar, no al importar el módulo: que falte la
    clave tiene que romper el vínculo con Alexa, no el arranque de toda la app.
    """


class DecryptionFailed(RuntimeError):
    """El texto cifrado no abre con la clave actual.

    En la práctica significa una sola cosa: la clave cambió desde que se guardó
    ese dato. No es un bug a depurar, es un vínculo que hay que rehacer.
    """


@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    """La instancia, construida una vez.

    `Fernet(...)` decodifica y valida la clave, y hacerlo por request sería
    trabajo repetido para llegar siempre al mismo objeto. El `lru_cache` también
    hace que una clave inválida se detecte la primera vez y no en cada llamada.
    """
    if not settings.TOKEN_ENCRYPTION_KEY:
        raise EncryptionUnavailable(
            "Falta TOKEN_ENCRYPTION_KEY: sin ella no se pueden guardar tokens."
        )
    try:
        return Fernet(settings.TOKEN_ENCRYPTION_KEY.encode())
    except (ValueError, TypeError) as exc:
        # El mensaje de `cryptography` habla de bytes en base64 url-safe y no
        # ayuda a nadie que no sepa de antemano qué formato esperaba.
        raise EncryptionUnavailable(
            "TOKEN_ENCRYPTION_KEY no es una clave Fernet válida. Generá una con "
            "`python -c 'from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())'`."
        ) from exc


def encryption_available() -> bool:
    """Si hay clave utilizable, para decidir si la feature está encendida.

    No cifra nada: es para que un endpoint pueda contestar "esto está apagado"
    antes de mandar al usuario a Amazon.
    """
    try:
        _fernet()
    except EncryptionUnavailable:
        return False
    return True


def encrypt_secret(plaintext: str) -> str:
    """Texto en claro -> texto cifrado listo para guardar en una columna."""
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt_secret(ciphertext: str) -> str:
    """La vuelta. Levanta `DecryptionFailed` si no abre."""
    try:
        return _fernet().decrypt(ciphertext.encode()).decode()
    except InvalidToken as exc:
        # Ni el texto cifrado ni la clave entran en el mensaje: este error
        # termina en un log, y es justo lo que estamos protegiendo.
        raise DecryptionFailed(
            "El dato guardado no abre con la TOKEN_ENCRYPTION_KEY actual."
        ) from exc
