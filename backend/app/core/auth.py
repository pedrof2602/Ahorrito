"""Quién sos: la dependencia que traduce una cookie en un usuario.

Todo endpoint que toque datos de alguien pide `CurrentUser`. No hay ninguna vía
por la que un repositorio de usuario obtenga un `user_id` que no haya salido de
acá: los repositorios lo exigen en el constructor y ya no tienen default, así
que un endpoint al que me olvide de pasarle el usuario no compila, en vez de
servir los datos del usuario 1 a quien pregunte.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Annotated

from fastapi import Depends, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_db
from app.core.security import (
    hash_session_token,
    new_session_token,
)
from app.db import tables as t
from app.db.repositories import AuthSessionRepository, UserRepository

DbSession = Annotated[AsyncSession, Depends(get_db)]

_UNAUTHENTICATED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Necesitás iniciar sesión.",
)


def _expiry(*, created_at=None):
    """Cuándo vence una sesión que se usa ahora.

    Dos relojes: el deslizante, que se corre en cada request, y el techo
    absoluto desde la creación. Gana el que caiga primero, así que una sesión
    muy usada igual muere a los 180 días.
    """
    now = t.utcnow()
    sliding = now + timedelta(seconds=settings.SESSION_TTL_S)
    if created_at is None:
        return sliding
    absolute = t.as_aware(created_at) + timedelta(
        seconds=settings.SESSION_ABSOLUTE_TTL_S
    )
    return min(sliding, absolute)


async def open_session(
    session: AsyncSession, user: t.User, *, response: Response, user_agent: str | None
) -> str:
    """Crea la sesión y planta la cookie. Devuelve el token en claro.

    El token en claro existe solo dentro de esta función y del `Set-Cookie`: lo
    que queda guardado es su SHA-256.
    """
    token = new_session_token()
    await AuthSessionRepository(session).create(
        user.id,
        hash_session_token(token),
        expires_at=_expiry(),
        user_agent=user_agent,
    )
    user.last_login_at = t.utcnow()
    set_session_cookie(response, token)
    return token


def set_session_cookie(response: Response, token: str) -> None:
    """`Set-Cookie` con los tres flags que hacen el trabajo.

    - `httponly`: el JavaScript de la página no puede leerla, así que un XSS no
      se lleva la sesión de recuerdo. Es la razón por la que esto no va en
      `localStorage`, que es exactamente lo que un XSS sí puede leer.
    - `secure`: no viaja por HTTP en claro.
    - `samesite`: el navegador no la manda en requests que originó otro sitio,
      que es lo que bloquea el CSRF sin necesidad de tokens anti-CSRF.
    """
    response.set_cookie(
        settings.COOKIE_NAME,
        token,
        max_age=settings.SESSION_TTL_S,
        httponly=True,
        secure=settings.COOKIE_SECURE,
        samesite=settings.COOKIE_SAMESITE,
        domain=settings.COOKIE_DOMAIN,
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    """Los atributos tienen que coincidir con los del `set_cookie` o el
    navegador borra *otra* cookie —una con distinto path o domain— y deja la
    buena en su lugar."""
    response.delete_cookie(
        settings.COOKIE_NAME,
        httponly=True,
        secure=settings.COOKIE_SECURE,
        samesite=settings.COOKIE_SAMESITE,
        domain=settings.COOKIE_DOMAIN,
        path="/",
    )


async def _resolve(
    request: Request, session: AsyncSession
) -> tuple[t.User, t.AuthSession] | None:
    token = request.cookies.get(settings.COOKIE_NAME)
    if not token:
        return None

    sessions = AuthSessionRepository(session)
    row = await sessions.by_token_hash(hash_session_token(token))
    if row is None:
        return None

    if t.as_aware(row.expires_at) <= t.utcnow():
        # Se borra en el acto en vez de esperar al purgado del arranque: la
        # request ya pagó el costo de encontrarla y dejarla es basura conocida.
        await sessions.delete(row)
        await session.commit()
        return None

    user = await UserRepository(session).by_id(row.user_id)
    if user is None or not user.is_active:
        return None

    await sessions.touch(row, expires_at=_expiry(created_at=row.created_at))
    await session.commit()
    return user, row


async def current_user(request: Request, session: DbSession) -> t.User:
    """El usuario de la request, o 401.

    Guarda la sesión en `request.state` para que un endpoint que necesite saber
    *cuál* de las sesiones del usuario es esta —cerrar las demás al cambiar la
    contraseña— no tenga que volver a resolver la cookie.
    """
    resolved = await _resolve(request, session)
    if resolved is None:
        raise _UNAUTHENTICATED
    user, auth_session = resolved
    request.state.auth_session = auth_session
    return user


async def optional_user(request: Request, session: DbSession) -> t.User | None:
    """Como `current_user` pero sin 401. Para endpoints que se comportan
    distinto con sesión y sin ella, en vez de negarse."""
    resolved = await _resolve(request, session)
    if resolved is None:
        return None
    user, auth_session = resolved
    request.state.auth_session = auth_session
    return user


CurrentUser = Annotated[t.User, Depends(current_user)]
OptionalUser = Annotated[t.User | None, Depends(optional_user)]


def require_role(*roles: str):
    """Dependencia que además exige un rol.

    Hoy no la usa ningún endpoint: elegiste un solo rol y todas las cuentas son
    `user`. Queda armada —y probada— porque el día que separes admin, el cambio
    es agregar `Depends(require_role("admin"))` a una ruta, no rehacer el
    esquema, migrar las sesiones abiertas ni reescribir el frontend.

        @router.post("/ingest", dependencies=[Depends(require_role("admin"))])
    """
    allowed = frozenset(roles)

    async def dependency(user: CurrentUser) -> t.User:
        if user.role not in allowed:
            # 403 y no 404: el recurso existe y sabemos quién sos, lo que falta
            # es permiso. Un 404 acá solo confunde al que depura.
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No tenés permiso para esto.",
            )
        return user

    return dependency
