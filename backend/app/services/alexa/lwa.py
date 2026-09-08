"""Login with Amazon: canjear el código, refrescar el token, y guardarlo.

El flujo completo, en tres pasos y dos de ellos acá:

1. `web/alexa.py` manda al usuario a la pantalla de autorización de Amazon.
2. Amazon lo devuelve con un `code` de un solo uso; `exchange_code()` lo canjea
   por un `access_token` (una hora) y un `refresh_token` (sin vencimiento).
3. `access_token()` es lo único que van a usar los endpoints de listas: devuelve
   un token válido y se encarga sola de refrescarlo cuando hace falta.

**Nada de esto loguea un token.** Se loguean el `user_id`, el resultado y el
código de error de Amazon; el cuerpo de una respuesta del endpoint de tokens no
entra nunca en un `log`, ni siquiera en `debug`, porque `debug` se enciende justo
el día que algo anda mal y queda encendido más de lo que se pensaba.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.crypto import encryption_available
from app.db import tables as t
from app.db.repositories import AlexaLinkRepository

log = logging.getLogger(__name__)

AUTHORIZE_URL = "https://www.amazon.com/ap/oa"
"""Dónde autoriza el usuario. Es el dominio global, que es el correcto para una
cuenta de Amazon argentina: los regionales (`amazon.co.jp`, `amazon.de`) son para
cuentas creadas en esos mercados y devuelven la cuenta equivocada, no un error."""

TOKEN_URL = "https://api.amazon.com/auth/o2/token"
"""Endpoint de tokens de LWA. Es global aunque el de autorización sea regional."""

SCOPES = ("alexa::household:lists:read", "alexa::household:lists:write")
"""Leer y escribir las listas de la casa.

Se piden los dos juntos y no de a uno a medida que hagan falta: cada scope nuevo
es una pantalla de autorización más para el usuario, y pedirle permiso dos veces
para lo que él entiende como "conectar Alexa" es peor que pedírselo una."""

REFRESH_SKEW_S = 60
"""Cuánto antes del vencimiento se considera vencido el `access_token`.

Sin margen, un token que vence en dos segundos pasa el control y llega a Amazon
vencido, y el error aparece en la llamada a la API de listas —lejos de acá— como
un 401 que parece un problema de permisos."""

_TIMEOUT_S = 15.0
"""Corto a propósito: hay una persona esperando un redirect del otro lado."""


class AlexaError(Exception):
    """Algo salió mal con el vínculo. Nunca lleva un token en el mensaje."""


class AlexaNotConfigured(AlexaError):
    """Falta `LWA_CLIENT_ID`, `LWA_CLIENT_SECRET`, `LWA_REDIRECT_URI` o
    `TOKEN_ENCRYPTION_KEY`. Es un problema del deploy, no del usuario."""


class AlexaNotLinked(AlexaError):
    """Este usuario nunca vinculó su cuenta. La respuesta es "vinculá", no un
    error: es el estado normal de cualquiera que todavía no pasó por el flujo."""


class AlexaLinkRevoked(AlexaError):
    """Amazon ya no acepta el `refresh_token`.

    Casi siempre porque el usuario revocó el permiso desde su cuenta de Amazon,
    que es exactamente lo que la política de privacidad le promete que puede
    hacer. Cuando esto pasa, la fila se borra: dejarla sería guardar una
    credencial muerta y seguir diciendo en la pantalla que el vínculo existe.
    """


class AlexaUnavailable(AlexaError):
    """Amazon no contestó, o contestó algo que no se puede usar. Reintentar
    puede servir; es lo que separa esto de `AlexaLinkRevoked`."""


@dataclass(slots=True, frozen=True)
class LwaTokens:
    """Lo que devuelve el endpoint de tokens, ya interpretado.

    `expires_in` (segundos) se convierte acá en un instante absoluto: es lo que
    se guarda, y hacerlo en el borde evita que la conversión se repita —con un
    reloj distinto cada vez— en cada lugar que la necesite.
    """

    access_token: str
    refresh_token: str
    expires_at: datetime
    scope: str


def is_configured() -> bool:
    """Si el vínculo con Alexa está encendido en este deploy.

    Incluye la clave de cifrado a propósito: sin ella el flujo llegaría hasta el
    final —el usuario autoriza en Amazon, vuelve— para recién ahí no poder
    guardar nada. Mejor no empezarlo.
    """
    return bool(
        settings.LWA_CLIENT_ID
        and settings.LWA_CLIENT_SECRET
        and settings.LWA_REDIRECT_URI
        and encryption_available()
    )


def authorize_url(state: str) -> str:
    """La URL de la pantalla de autorización de Amazon.

    Los scopes van separados por espacios, como manda OAuth 2.0; `httpx` los
    codifica. Armar el query a mano con f-strings es el camino corto a un
    `redirect_uri` mal escapado que Amazon rechaza con `invalid_client`.
    """
    if not is_configured():
        raise AlexaNotConfigured("Falta configurar Login with Amazon.")

    params = {
        "client_id": settings.LWA_CLIENT_ID,
        "scope": " ".join(SCOPES),
        "response_type": "code",
        "redirect_uri": settings.LWA_REDIRECT_URI,
        "state": state,
    }
    return str(httpx.URL(AUTHORIZE_URL, params=params))


async def _post_token(form: dict[str, str], *, what: str) -> LwaTokens:
    """El POST al endpoint de tokens, que es idéntico para los dos grants.

    Cliente propio y no `services/http.py`: aquel `parse_json()` levanta
    `ProviderBadRequest` ante cualquier body que traiga una clave `error`, y las
    respuestas de error de OAuth **son** un JSON con `error`. Pasarlas por ahí
    perdería justo el dato que hay que leer para distinguir un `invalid_grant`
    —el usuario revocó, hay que desvincular— de cualquier otra cosa.
    """
    form = {
        **form,
        "client_id": settings.LWA_CLIENT_ID,
        "client_secret": settings.LWA_CLIENT_SECRET,
    }

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
            response = await client.post(
                TOKEN_URL,
                data=form,
                headers={"Accept": "application/json"},
            )
    except httpx.HTTPError as exc:
        # `exc` no lleva credenciales (httpx no vuelca el body en el mensaje),
        # pero se registra el tipo y no el objeto por las dudas.
        log.warning("LWA %s: no se pudo hablar con Amazon (%s)", what, type(exc).__name__)
        raise AlexaUnavailable("No se pudo contactar a Amazon.") from exc

    try:
        body = response.json()
    except ValueError as exc:
        log.error("LWA %s: Amazon contestó %s con algo que no es JSON", what, response.status_code)
        raise AlexaUnavailable("Amazon respondió algo inesperado.") from exc

    if response.status_code != httpx.codes.OK:
        # `error` y `error_description` son campos del estándar y no traen
        # secretos: son seguros de loguear, y son lo único que explica el fallo.
        code = body.get("error", "desconocido")
        log.warning(
            "LWA %s: Amazon rechazó la petición (%s: %s)",
            what,
            code,
            body.get("error_description", ""),
        )
        if code == "invalid_grant":
            raise AlexaLinkRevoked("Amazon ya no acepta esta autorización.")
        raise AlexaUnavailable(f"Amazon rechazó la petición ({code}).")

    try:
        return LwaTokens(
            access_token=body["access_token"],
            refresh_token=body["refresh_token"],
            expires_at=t.utcnow() + timedelta(seconds=int(body["expires_in"])),
            scope=body.get("scope", " ".join(SCOPES)),
        )
    except (KeyError, TypeError, ValueError) as exc:
        # Un 200 sin `refresh_token` no es un caso hipotético: pasa si el
        # Security Profile no tiene habilitado el grant. Sin él no hay vínculo
        # que dure más de una hora, así que es un fallo y no un token a medias.
        log.error("LWA %s: falta un campo en la respuesta de Amazon", what)
        raise AlexaUnavailable("Amazon respondió sin los datos esperados.") from exc


async def exchange_code(code: str) -> LwaTokens:
    """Canjea el `code` del callback por los tokens. El `code` es de un solo uso
    y vive unos minutos: no se guarda ni se reintenta."""
    if not is_configured():
        raise AlexaNotConfigured("Falta configurar Login with Amazon.")

    return await _post_token(
        {
            "grant_type": "authorization_code",
            "code": code,
            # Amazon exige que sea el mismo con el que se pidió el código, y lo
            # compara literal: es parte de lo que ata el código a esta app.
            "redirect_uri": settings.LWA_REDIRECT_URI,
        },
        what="canje",
    )


async def refresh(refresh_token: str) -> LwaTokens:
    """Un `access_token` nuevo a partir del `refresh_token`.

    Amazon suele devolver el mismo `refresh_token`, pero devuelve uno y hay que
    guardar el que devuelve: asumir que no rota es la clase de suposición que
    funciona durante un año y después deja a todos desvinculados de golpe.
    """
    if not is_configured():
        raise AlexaNotConfigured("Falta configurar Login with Amazon.")

    return await _post_token(
        {"grant_type": "refresh_token", "refresh_token": refresh_token},
        what="refresco",
    )


async def link(session: AsyncSession, user_id: int, tokens: LwaTokens) -> None:
    """Guarda —o pisa— el vínculo del usuario. No hace `commit`: eso es del
    endpoint, que puede tener más cosas en la misma transacción."""
    await AlexaLinkRepository(session, user_id).upsert(
        access_token=tokens.access_token,
        refresh_token=tokens.refresh_token,
        expires_at=tokens.expires_at,
        scope=tokens.scope,
    )
    log.info("Vínculo con Alexa guardado para el usuario %d", user_id)


async def access_token(session: AsyncSession, user_id: int) -> str:
    """Un `access_token` válido para este usuario, refrescándolo si hace falta.

    Es la única función que van a llamar los endpoints de listas. Todo lo demás
    de este módulo existe para que esta pueda devolver un string y ya.

    Hace `commit` cuando refresca, y a propósito: el token nuevo tiene que
    quedar guardado aunque la operación que lo pidió falle después. Si no, cada
    intento fallido volvería a gastar un refresco contra Amazon.
    """
    links = AlexaLinkRepository(session, user_id)
    row = await links.get()
    if row is None:
        raise AlexaNotLinked("Este usuario no vinculó su cuenta de Amazon.")

    current, refresh_token = links.tokens(row)

    # `as_aware` no es opcional: SQLite devuelve el datetime sin tzinfo y
    # compararlo contra `utcnow()` tira TypeError. Mismo motivo por el que
    # `core/auth.py` lo usa para el vencimiento de las sesiones.
    remaining = t.as_aware(row.expires_at) - t.utcnow()
    if remaining > timedelta(seconds=REFRESH_SKEW_S):
        return current

    try:
        tokens = await refresh(refresh_token)
    except AlexaLinkRevoked:
        # El usuario revocó desde amazon.com. Se borra la fila acá y no en el
        # llamador porque es la promesa de la política de privacidad —"a partir
        # de ahí dejamos de tener acceso"— y no puede depender de que cada
        # llamador se acuerde de cumplirla.
        await links.delete()
        await session.commit()
        log.info("Vínculo con Alexa revocado desde Amazon: usuario %d", user_id)
        raise

    await links.upsert(
        access_token=tokens.access_token,
        refresh_token=tokens.refresh_token,
        expires_at=tokens.expires_at,
        scope=tokens.scope,
    )
    await session.commit()
    return tokens.access_token
