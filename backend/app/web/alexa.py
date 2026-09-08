"""El flujo OAuth de "Vincular cuenta de Alexa", del lado del navegador.

Estas dos rutas viven en la raíz y no bajo `/api/v1` por una razón que no se
puede negociar: el `Redirect URI` está registrado en el Security Profile de
Amazon como `https://ahorrito.fly.dev/auth/alexa/callback`, Amazon lo compara
carácter por carácter, y cambiarlo del lado de Amazon rompe el vínculo de todos
los que ya vincularon. La URL manda sobre la organización del código.

De ahí salen las otras dos decisiones:

* **Se registran antes del `mount` del frontend**, igual que la política de
  privacidad. El `StaticFiles(html=True)` de `/` se queda con todo lo que no
  reclamó una ruta anterior, así que registrarlas después las volvería un 404
  disfrazado de `index.html`.
* **Ninguna contesta JSON.** Del otro lado hay una persona mirando una pantalla,
  no un `fetch`: cada rama —incluidas las de error— termina en un redirect a la
  app con un código en el query, y la app lo traduce a una frase.

Sobre la puerta de acceso (`core/gate.py`): estas rutas **no** están exentas, y
está bien. La vuelta desde Amazon es una navegación de nivel superior del mismo
navegador que empezó el flujo, así que la cookie `compras_access` viaja con ella.
Lo que sí es sutil es que la *cookie de sesión* también llega: `SameSite=lax` se
manda en un GET de navegación aunque lo origine otro sitio, y ese detalle es todo
lo que permite que el callback sepa a qué usuario atar el token. Se rompería si
la cookie pasara a `strict`, o si Amazon volviera por POST —no hace ninguna de
las dos cosas, pero conviene saber qué mirar el día que esto deje de andar—.
"""

from __future__ import annotations

import logging
import secrets
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import OptionalUser
from app.core.config import settings
from app.core.db import get_db
from app.services.alexa import lwa

log = logging.getLogger(__name__)

DbSession = Annotated[AsyncSession, Depends(get_db)]

router = APIRouter(prefix="/auth/alexa", include_in_schema=False)
"""Fuera del OpenAPI: no son endpoints para llamar desde ningún cliente, son dos
pasos de un flujo que empieza y termina en el navegador."""

STATE_COOKIE = "alexa_oauth_state"
STATE_TTL_S = 600
"""Diez minutos para autorizar en Amazon. Alcanza de sobra para leer la pantalla
y decidir, y acota la ventana en la que un `state` filtrado sirve de algo."""

APP_URL = "/"
"""A dónde se vuelve siempre. La app es de una sola pantalla; el resultado del
flujo llega como `?alexa=<código>` y lo interpreta el frontend."""


def _back(outcome: str) -> RedirectResponse:
    """Vuelta a la app con el resultado en el query.

    303 y no 307: el navegador tiene que hacer un GET limpio de la app. Un 307
    conserva el método, que acá da lo mismo, pero además deja el callback en el
    historial con el `code` puesto — y ese `code` no tiene por qué quedar en la
    barra de direcciones de nadie.
    """
    return RedirectResponse(
        f"{APP_URL}?alexa={outcome}", status_code=status.HTTP_303_SEE_OTHER
    )


def _clear_state(response: Response) -> None:
    """Borra la cookie de `state`.

    Los atributos tienen que coincidir con los del `set_cookie` o el navegador
    borra *otra* cookie y deja la buena donde estaba — el mismo cuidado que
    `core/auth.py::clear_session_cookie`.
    """
    response.delete_cookie(
        STATE_COOKIE,
        httponly=True,
        secure=settings.COOKIE_SECURE,
        samesite=settings.COOKIE_SAMESITE,
        domain=settings.COOKIE_DOMAIN,
        path=router.prefix,
    )


@router.get("/login")
async def login(user: OptionalUser) -> RedirectResponse:
    """Arranca el flujo: manda al usuario a autorizar en Amazon.

    `OptionalUser` y no `CurrentUser` porque un 401 en JSON acá es una pantalla
    en blanco con una llave entre corchetes. Sin sesión se vuelve a la app, que
    ya sabe mostrar el login.
    """
    if user is None:
        return _back("sesion")

    if not lwa.is_configured():
        # Pasa en desarrollo, donde no hay secrets. Mejor decirlo antes que
        # mandar a alguien a una pantalla de Amazon que termina en error.
        log.warning("Vínculo con Alexa pedido pero LWA no está configurado.")
        return _back("sin_config")

    state = secrets.token_urlsafe(32)
    response = RedirectResponse(
        lwa.authorize_url(state), status_code=status.HTTP_307_TEMPORARY_REDIRECT
    )
    response.set_cookie(
        STATE_COOKIE,
        state,
        max_age=STATE_TTL_S,
        httponly=True,
        secure=settings.COOKIE_SECURE,
        samesite=settings.COOKIE_SAMESITE,
        domain=settings.COOKIE_DOMAIN,
        # Acotada a este flujo: no hay ninguna otra ruta que la necesite, y una
        # cookie que no se manda es una cookie que no se filtra en un log de
        # proxy ni viaja en cada request de la app.
        path=router.prefix,
    )
    return response


@router.get("/callback")
async def callback(
    request: Request,
    session: DbSession,
    user: OptionalUser,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> RedirectResponse:
    """La vuelta desde Amazon: valida, canjea y guarda.

    El orden de los controles importa y no es arbitrario: **el `state` se valida
    antes de canjear nada**. Es el control que sostiene todo el flujo, y sin él
    la vulnerabilidad no es teórica: un atacante consigue un `code` con *su*
    cuenta de Amazon, hace que el navegador de un usuario logueado visite este
    callback con ese código —basta un `<img>` en cualquier página—, y el usuario
    queda con la cuenta de Amazon del atacante vinculada a la suya. A partir de
    ahí lo que la app escriba "en las listas del usuario" aparece en el Echo del
    atacante, y lo que el atacante escriba aparece en la app del usuario.
    """
    response: RedirectResponse

    if error:
        # `access_denied` es el usuario apretando "Cancelar" en Amazon. No es un
        # fallo: es una respuesta, y merece un mensaje distinto al de un error.
        log.info("Vínculo con Alexa no autorizado por el usuario (%s)", error)
        response = _back("cancelado" if error == "access_denied" else "error")

    elif not state or not code:
        log.warning("Callback de Alexa sin `state` o sin `code`.")
        response = _back("error")

    elif not _state_ok(request, state):
        log.warning("Callback de Alexa con `state` que no coincide: se descarta.")
        response = _back("error")

    elif user is None:
        # La sesión venció durante el paseo por Amazon. El `code` se tira sin
        # canjear: no hay a qué usuario atar el token, y canjearlo para después
        # descartarlo solo gastaría un código de un solo uso.
        log.info("Callback de Alexa sin sesión: el código se descarta.")
        response = _back("sesion")

    else:
        response = await _finish(session, user.id, code)

    _clear_state(response)
    return response


def _state_ok(request: Request, state: str) -> bool:
    """Compara el `state` del query contra el de la cookie.

    `compare_digest` y no `==` por el mismo motivo que en `core/gate.py`:
    comparar strings corta en el primer byte distinto y ese tiempo es medible.
    Acá importa menos que allá —el `state` vive diez minutos— pero cuesta igual.
    """
    expected = request.cookies.get(STATE_COOKIE)
    return bool(expected) and secrets.compare_digest(expected, state)


async def _finish(
    session: AsyncSession, user_id: int, code: str
) -> RedirectResponse:
    """Canje y guardado, con los errores de Amazon traducidos a la pantalla."""
    try:
        tokens = await lwa.exchange_code(code)
        await lwa.link(session, user_id, tokens)
        await session.commit()
    except lwa.AlexaNotConfigured:
        log.error("Callback de Alexa con LWA sin configurar.")
        return _back("sin_config")
    except lwa.AlexaError as exc:
        # `exc` no lleva tokens: `lwa` construye sus mensajes con el código de
        # error de Amazon y nada más.
        log.warning("No se pudo vincular Alexa para el usuario %d: %s", user_id, exc)
        return _back("error")

    return _back("ok")
