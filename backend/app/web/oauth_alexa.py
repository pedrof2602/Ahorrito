"""Ahorrito como proveedor OAuth 2.0, para el account linking del skill.

Es el flujo **inverso** al de `web/alexa.py`: allá esta app le pedía tokens a
Amazon, acá Amazon le pide tokens a esta app. Vale insistir porque los dos hablan
de "vincular Alexa" y tienen endpoints que se llaman parecido.

El recorrido completo:

1. El usuario aprieta "Vincular" en la app de Alexa, que abre
   `GET /oauth/alexa/authorize` en un navegador embebido.
2. Ese navegador **no tiene la cookie de sesión de Ahorrito** —es otro contexto—
   así que se le pide email y contraseña. Es el motivo de que exista una segunda
   pantalla de login en un proyecto que ya tiene una.
3. Con las credenciales válidas se emite un `code` y se redirige a la URL de
   Amazon que vino en `redirect_uri`.
4. Los servidores de Amazon canjean ese `code` en `POST /oauth/alexa/token` por
   un par de tokens, presentando el `client_secret`.
5. De ahí en adelante, cada frase del usuario llega a `/alexa/skill` con el
   `access_token` puesto.

**Lo más importante de este archivo es el manejo de `redirect_uri`.** Es a donde
se manda el `code`, o sea la cuenta: si se acepta cualquiera, alcanza con que
alguien le pase al usuario un link con su propio `redirect_uri` para quedarse con
el vínculo. Por eso se compara contra una whitelist literal, por eso un
`redirect_uri` inválido nunca produce un redirect —se muestra un error acá
mismo— y por eso se vuelve a validar en el POST aunque ya se haya validado en el
GET: lo que vuelve del formulario lo escribe el navegador, no nosotros.
"""

from __future__ import annotations

import base64
import hashlib
import html
import logging
import secrets
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Request, Response, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.auth import _login_limiter
from app.core.config import settings
from app.core.db import get_db
from app.core.ratelimit import client_ip
from app.core.security import UNUSABLE_PASSWORD, hash_password, verify_password
from app.db.repositories import (
    AlexaSkillCodeRepository,
    AlexaSkillTokenRepository,
    UserRepository,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/oauth/alexa", include_in_schema=False)

OAUTH_ALEXA_PATHS = frozenset(
    {"/oauth/alexa/authorize", "/oauth/alexa/token"}
)
"""Lo que la puerta de acceso deja pasar, por lo mismo que `SKILL_PATHS`.

Ni el webview de la app de Alexa ni los servidores de Amazon tienen cómo
presentar la `ACCESS_KEY`. Que estén exentos no los deja abiertos: el authorize
pide email y contraseña, y el token pide el `client_secret`.
"""

DbSession = Annotated[AsyncSession, Depends(get_db)]

_PAGE = (Path(__file__).with_name("oauth_alexa.html")).read_text(encoding="utf-8")

_DUMMY_HASH = hash_password("contraseña que no es de nadie")
"""El mismo truco que `api/v1/auth.py`: cuando el email no existe se verifica
igual contra este hash, para que el caso "no hay cuenta" tarde lo mismo que el
caso "contraseña incorrecta". Sin eso, este formulario —que es público y no tiene
rate limit por cuenta— se convierte en la forma más cómoda de averiguar quién
tiene cuenta acá."""


def is_configured() -> bool:
    """Si el account linking está encendido en este deploy."""
    return bool(
        settings.ALEXA_LINK_CLIENT_ID
        and settings.ALEXA_LINK_CLIENT_SECRET
        and settings.ALEXA_LINK_REDIRECT_URIS
    )


# ------------------------------------------------------------------ authorize


def _page(
    *,
    client_id: str,
    redirect_uri: str,
    state: str,
    code_challenge: str,
    code_challenge_method: str,
    error: str = "",
) -> HTMLResponse:
    """El formulario, con los valores del flujo incrustados.

    `html.escape(quote=True)` en **todos** los valores, sin excepción: `state` y
    `redirect_uri` los elige quien arma la URL, y van a parar adentro de un
    atributo HTML. Sin escapar, un `state` con comillas cierra el atributo y
    escribe markup en la página de login, que es el peor lugar posible para un
    XSS porque es justo donde el usuario tipea la contraseña.
    """
    banner = f'<p class="error" role="alert">{html.escape(error)}</p>' if error else ""

    filled = _PAGE
    for token, value in (
        ("{{ERROR}}", banner),
        ("{{CLIENT_ID}}", html.escape(client_id, quote=True)),
        ("{{REDIRECT_URI}}", html.escape(redirect_uri, quote=True)),
        ("{{STATE}}", html.escape(state, quote=True)),
        ("{{CODE_CHALLENGE}}", html.escape(code_challenge, quote=True)),
        ("{{CODE_CHALLENGE_METHOD}}", html.escape(code_challenge_method, quote=True)),
    ):
        filled = filled.replace(token, value)

    # El banner de error ya viene armado y escapado; el resto se escapó recién.
    return HTMLResponse(filled)


def _refuse(message: str) -> HTMLResponse:
    """Un error que se muestra acá en vez de redirigir.

    Es para cuando `client_id` o `redirect_uri` no cierran. En ese caso **no se
    puede redirigir a ningún lado**: el único destino que tendríamos es el que
    vino en el request, que es justo el que no confiamos. Redirigir ahí sería
    entregarle al atacante el error —y, si el flujo siguiera, el código—.
    """
    return HTMLResponse(
        "<!doctype html><html lang=es><meta charset=utf-8>"
        "<title>No se pudo vincular</title>"
        "<body style='font:16px/1.5 system-ui;max-width:32rem;margin:15vh auto;padding:0 1rem'>"
        f"<h1 style='font-size:1.2rem'>No se pudo iniciar la vinculación</h1><p>{html.escape(message)}</p>"
        "<p style='color:#666'>Volvé a intentarlo desde la app de Alexa.</p>",
        status_code=status.HTTP_400_BAD_REQUEST,
    )


def _check_client(client_id: str, redirect_uri: str) -> str | None:
    """Valida el cliente y el destino. Devuelve el error, o `None` si está bien."""
    if not is_configured():
        return "La vinculación con Alexa no está configurada en este servidor."
    if not secrets.compare_digest(client_id, settings.ALEXA_LINK_CLIENT_ID):
        return "La aplicación que pidió vincular no está autorizada."
    if redirect_uri not in settings.ALEXA_LINK_REDIRECT_URIS:
        # Se loguea porque en la práctica el 99% de las veces es un error de
        # configuración nuestro —la URL de la consola cambió, o falta una
        # región— y sin el valor concreto es imposible de diagnosticar.
        log.warning("Alexa: redirect_uri no autorizado: %r", redirect_uri)
        return "La dirección de retorno no está autorizada."
    return None


@router.get("/authorize")
async def authorize_form(
    client_id: str = "",
    redirect_uri: str = "",
    response_type: str = "",
    state: str = "",
    code_challenge: str = "",
    code_challenge_method: str = "",
    scope: str = "",  # noqa: ARG001
) -> Response:
    """La pantalla de login del vínculo.

    `scope` se declara y se ignora: Alexa lo manda si está configurado en la
    consola, y un parámetro no declarado haría que FastAPI lo rechace. Acá no
    hay permisos parciales —vincular es vincular— así que no hay nada que leer.
    """
    problem = _check_client(client_id, redirect_uri)
    if problem:
        return _refuse(problem)

    if response_type != "code":
        return _refuse("Solo se admite el flujo de código de autorización.")

    if code_challenge and code_challenge_method != "S256":
        # `plain` es PKCE sin la parte que sirve: el `code_verifier` viaja tal
        # cual y quien intercepte el authorize se lo queda. Alexa manda S256.
        return _refuse("Solo se admite PKCE con S256.")

    return _page(
        client_id=client_id,
        redirect_uri=redirect_uri,
        state=state,
        code_challenge=code_challenge,
        code_challenge_method=code_challenge_method,
    )


@router.post("/authorize")
async def authorize_submit(
    request: Request,
    session: DbSession,
    email: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
    client_id: Annotated[str, Form()] = "",
    redirect_uri: Annotated[str, Form()] = "",
    state: Annotated[str, Form()] = "",
    code_challenge: Annotated[str, Form()] = "",
    code_challenge_method: Annotated[str, Form()] = "",
) -> Response:
    """Verifica las credenciales y devuelve el `code` a Amazon."""
    # Se revalida todo aunque venga del formulario que servimos nosotros: lo que
    # llega en un POST lo controla el cliente, siempre.
    problem = _check_client(client_id, redirect_uri)
    if problem:
        return _refuse(problem)

    ip = client_ip(request)
    if _login_limiter.is_blocked(ip):  # noqa: SLF001
        return _page(
            client_id=client_id,
            redirect_uri=redirect_uri,
            state=state,
            code_challenge=code_challenge,
            code_challenge_method=code_challenge_method,
            error="Demasiados intentos fallidos. Probá de nuevo en un rato.",
        )

    user = await UserRepository(session).by_email(email)

    # Mismo orden que `api/v1/auth.py:login`: se verifica siempre, contra el
    # hash real o contra el de descarte, para que los dos casos tarden igual.
    stored = user.password_hash if user is not None else _DUMMY_HASH
    ok = verify_password(password, stored)

    if not ok or user is None or not user.is_active or user.password_hash == UNUSABLE_PASSWORD:
        _login_limiter.record_failure(ip)
        return _page(
            client_id=client_id,
            redirect_uri=redirect_uri,
            state=state,
            code_challenge=code_challenge,
            code_challenge_method=code_challenge_method,
            error="Email o contraseña incorrectos.",
        )

    _login_limiter.reset(ip)

    codes = AlexaSkillCodeRepository(session)
    await codes.purge_expired()
    code = await codes.issue(
        user.id, redirect_uri=redirect_uri, code_challenge=code_challenge or None
    )
    await session.commit()

    params = {"code": code}
    if state:
        params["state"] = state

    log.info("Alexa: código de vinculación emitido para el usuario %d", user.id)
    # 303 para que el navegador haga GET: el destino es de Amazon y no espera un
    # POST, y sin esto un refresh reenviaría la contraseña.
    return RedirectResponse(
        f"{redirect_uri}?{urlencode(params)}", status_code=status.HTTP_303_SEE_OTHER
    )


# ---------------------------------------------------------------------- token


def _oauth_error(code: str, http_status: int = status.HTTP_400_BAD_REQUEST) -> JSONResponse:
    """El formato de error que define OAuth 2.0.

    Amazon lee el campo `error` para decidir qué hacer: ante `invalid_grant`
    considera el vínculo muerto y le vuelve a pedir al usuario que vincule, que
    es exactamente lo que queremos cuando un `refresh_token` ya no existe.
    """
    return JSONResponse({"error": code}, status_code=http_status)


def _client_credentials(
    request: Request, client_id: str, client_secret: str
) -> tuple[str, str]:
    """Las credenciales del cliente, del header `Authorization` o del cuerpo.

    OAuth 2.0 define las dos formas y dice que el header es la preferida. Amazon
    usa una u otra según cómo esté configurado el skill en la consola, así que
    hay que aceptar ambas o el canje falla con un `invalid_client` que no explica
    que el problema es dónde venían las credenciales.
    """
    header = request.headers.get("Authorization", "")
    if header.startswith("Basic "):
        try:
            decoded = base64.b64decode(header[6:], validate=True).decode("utf-8")
            in_header_id, _, in_header_secret = decoded.partition(":")
        except (ValueError, UnicodeDecodeError):
            return "", ""
        return in_header_id, in_header_secret
    return client_id, client_secret


def _pkce_ok(challenge: str | None, verifier: str) -> bool:
    """`BASE64URL(SHA256(verifier)) == challenge`, sin padding.

    Si el código se emitió sin challenge no hay nada que verificar y se acepta;
    en la práctica Alexa siempre manda uno, así que ese camino es para un cliente
    de prueba, no para producción.
    """
    if not challenge:
        return True
    if not verifier:
        return False
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return secrets.compare_digest(expected, challenge)


@router.post("/token")
async def token(
    request: Request,
    session: DbSession,
    grant_type: Annotated[str, Form()] = "",
    code: Annotated[str, Form()] = "",
    redirect_uri: Annotated[str, Form()] = "",
    code_verifier: Annotated[str, Form()] = "",
    refresh_token: Annotated[str, Form()] = "",
    client_id: Annotated[str, Form()] = "",
    client_secret: Annotated[str, Form()] = "",
) -> Response:
    """Canjea un `code` —o un `refresh_token`— por un par de tokens."""
    if not is_configured():
        return _oauth_error("invalid_client", status.HTTP_401_UNAUTHORIZED)

    got_id, got_secret = _client_credentials(request, client_id, client_secret)
    if not (
        secrets.compare_digest(got_id, settings.ALEXA_LINK_CLIENT_ID)
        and secrets.compare_digest(got_secret, settings.ALEXA_LINK_CLIENT_SECRET)
    ):
        log.warning("Alexa: /token con credenciales de cliente inválidas")
        return _oauth_error("invalid_client", status.HTTP_401_UNAUTHORIZED)

    tokens = AlexaSkillTokenRepository(session)

    if grant_type == "authorization_code":
        # `consume` borra el código pase lo que pase: un código presentado una
        # vez no vuelve al pozo aunque el canje falle después.
        row = await AlexaSkillCodeRepository(session).consume(code)
        await session.commit()

        if row is None:
            log.warning("Alexa: /token con código inexistente o vencido")
            return _oauth_error("invalid_grant")

        if row.redirect_uri != redirect_uri:
            log.warning("Alexa: /token con redirect_uri distinto al del authorize")
            return _oauth_error("invalid_grant")

        if not _pkce_ok(row.code_challenge, code_verifier):
            log.warning("Alexa: /token con code_verifier que no cierra")
            return _oauth_error("invalid_grant")

        access, refresh = await tokens.issue(row.user_id)
        await session.commit()
        log.info("Alexa: vínculo creado para el usuario %d", row.user_id)
        return _token_response(access, refresh)

    if grant_type == "refresh_token":
        pair = await tokens.rotate(refresh_token)
        await session.commit()
        if pair is None:
            # El vínculo ya no existe: el usuario desvinculó desde la app. Con
            # `invalid_grant` Amazon deja de reintentar y le pide vincular.
            log.info("Alexa: refresh de un vínculo que ya no está")
            return _oauth_error("invalid_grant")
        return _token_response(*pair)

    return _oauth_error("unsupported_grant_type")


def _token_response(access: str, refresh: str) -> JSONResponse:
    payload: dict[str, Any] = {
        "access_token": access,
        "refresh_token": refresh,
        "token_type": "Bearer",
        "expires_in": settings.ALEXA_TOKEN_TTL_S,
    }
    # `no-store` es del RFC de OAuth y acá no es ceremonia: sin él, un proxy que
    # cachee esta respuesta le sirve el token de un usuario al siguiente.
    return JSONResponse(payload, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})
