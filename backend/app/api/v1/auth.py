"""Registro, login, logout y cambio de contraseña.

Una regla atraviesa todo el archivo: **ninguna respuesta dice si una cuenta
existe.** Login con email inexistente y login con contraseña equivocada
devuelven el mismo 401 con el mismo texto, y los dos tardan lo mismo porque el
caso "no existe" igual paga un hash de Argon2 contra un valor fijo. Sin eso,
cualquiera puede usar el formulario de login para averiguar quién tiene cuenta
acá, que en una app que guarda domicilios y DNI no es un detalle.

El registro es la excepción inevitable: tiene que poder decir "ese email ya está
tomado" o no hay forma de registrarse. Es el precio del registro abierto.
"""

from __future__ import annotations

import re
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import (
    CurrentUser,
    clear_session_cookie,
    open_session,
)
from app.core.config import settings
from app.core.db import get_db
from app.core.ratelimit import AttemptLimiter, client_ip
from app.core.security import (
    UNUSABLE_PASSWORD,
    hash_password,
    hash_session_token,
    needs_rehash,
    normalize_email,
    verify_password,
)
from app.db.repositories import AuthSessionRepository, UserRepository

router = APIRouter(prefix="/auth", tags=["cuenta"])

DbSession = Annotated[AsyncSession, Depends(get_db)]

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

_login_limiter = AttemptLimiter(
    settings.LOGIN_MAX_ATTEMPTS, settings.LOGIN_ATTEMPT_WINDOW_S
)

_INVALID_CREDENTIALS = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Email o contraseña incorrectos.",
)

_DUMMY_HASH = hash_password("contraseña que no es de nadie")
"""Hash contra el que se verifica cuando el email no existe.

Sin esto, un login con email inexistente vuelve en microsegundos y uno con email
real tarda los ~50 ms de Argon2. Esa diferencia es medible desde afuera y
convierte al endpoint en un buscador de cuentas. Se calcula una vez al importar
el módulo, no por request."""


class Credentials(BaseModel):
    email: str = Field(..., max_length=254, examples=["ejemplo@ejemplo.com"])
    password: str = Field(..., max_length=1024)
    """Con techo de largo: Argon2 hashea lo que le den, y sin límite un POST de
    10 MB de contraseña es CPU regalada a quien la mande."""

    @field_validator("email", mode="after")
    @classmethod
    def _normalize(cls, value: str) -> str:
        value = normalize_email(value)
        if not _EMAIL_RE.match(value):
            raise ValueError("Ese email no tiene forma de email.")
        return value


class Registration(Credentials):
    @field_validator("password", mode="after")
    @classmethod
    def _strong_enough(cls, value: str) -> str:
        """Solo largo mínimo.

        Nada de "una mayúscula, un número y un símbolo": esas reglas empujan a
        `Password1!`, que es corta y está en todos los diccionarios, y el NIST
        las desaconseja explícitamente desde 2017. El largo es lo que encarece
        el ataque.
        """
        if len(value) < settings.PASSWORD_MIN_LENGTH:
            raise ValueError(
                f"La contraseña necesita al menos {settings.PASSWORD_MIN_LENGTH} caracteres."
            )
        return value


class PasswordChange(BaseModel):
    current_password: str = Field(..., max_length=1024)
    new_password: str = Field(..., max_length=1024)

    @field_validator("new_password", mode="after")
    @classmethod
    def _strong_enough(cls, value: str) -> str:
        if len(value) < settings.PASSWORD_MIN_LENGTH:
            raise ValueError(
                f"La contraseña necesita al menos {settings.PASSWORD_MIN_LENGTH} caracteres."
            )
        return value


class UserOut(BaseModel):
    """Lo que la app sabe del usuario logueado.

    No incluye `password_hash` ni el id de sesión. Es un modelo explícito y no
    un `from_attributes` sobre la fila entera justamente para que agregar una
    columna sensible a `users` no la publique sola.
    """

    id: int
    email: str
    role: str


def _out(user) -> UserOut:
    return UserOut(id=user.id, email=user.email, role=user.role)


@router.post(
    "/register",
    response_model=UserOut,
    status_code=status.HTTP_201_CREATED,
    summary="Crear una cuenta",
)
async def register(
    request: Request,
    response: Response,
    session: DbSession,
    payload: Registration,
) -> UserOut:
    """Crea la cuenta y deja la sesión abierta.

    Loguea de una en vez de mandar al formulario de login: el usuario ya probó
    que sabe la contraseña al escribirla, y hacérsela escribir de nuevo no
    verifica nada.
    """
    if not settings.REGISTRATION_OPEN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="El registro está cerrado.",
        )

    users = UserRepository(session)
    try:
        user = await users.create(payload.email, hash_password(payload.password))
        await session.flush()
    except IntegrityError as exc:
        # La condición de carrera es real: dos registros simultáneos con el
        # mismo email pasan los dos el chequeo previo y solo uno sobrevive al
        # índice único. Por eso se atrapa la excepción en vez de preguntar antes.
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Ya hay una cuenta con ese email.",
        ) from exc

    await open_session(
        session, user, response=response, user_agent=request.headers.get("user-agent")
    )
    await session.commit()
    return _out(user)


@router.post("/login", response_model=UserOut, summary="Iniciar sesión")
async def login(
    request: Request,
    response: Response,
    session: DbSession,
    payload: Credentials,
) -> UserOut:
    ip = client_ip(request)
    if _login_limiter.is_blocked(ip):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Demasiados intentos fallidos. Probá de nuevo en un rato.",
            headers={"Retry-After": str(_login_limiter.retry_after_s(ip))},
        )

    user = await UserRepository(session).by_email(payload.email)

    # El orden importa: se verifica *siempre*, contra el hash real si la cuenta
    # existe y contra uno de descarte si no. Cortar antes con un `if user is
    # None` haría que el caso inexistente vuelva sin pagar Argon2, y esa
    # diferencia de tiempo es la fuga que este endpoint trata de no tener.
    stored = user.password_hash if user is not None else _DUMMY_HASH
    ok = verify_password(payload.password, stored)

    if not ok or user is None or not user.is_active:
        _login_limiter.record_failure(ip)
        raise _INVALID_CREDENTIALS

    if needs_rehash(user.password_hash):
        # Único momento en que la contraseña en claro está disponible para
        # regrabarla con los parámetros actuales de Argon2.
        user.password_hash = hash_password(payload.password)

    _login_limiter.reset(ip)
    await open_session(
        session, user, response=response, user_agent=request.headers.get("user-agent")
    )
    await session.commit()
    return _out(user)


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Cerrar esta sesión",
)
async def logout(request: Request, response: Response, session: DbSession) -> None:
    """Borra la sesión del servidor y la cookie del navegador.

    Sin `CurrentUser` a propósito: cerrar sesión con una cookie ya vencida tiene
    que limpiar el navegador igual, y un 401 acá dejaría al usuario con una
    cookie muerta que no puede sacarse de encima.
    """
    token = request.cookies.get(settings.COOKIE_NAME)
    if token:
        sessions = AuthSessionRepository(session)
        row = await sessions.by_token_hash(hash_session_token(token))
        if row is not None:
            await sessions.delete(row)
            await session.commit()
    clear_session_cookie(response)


@router.get("/me", response_model=UserOut, summary="Quién soy")
async def me(user: CurrentUser) -> UserOut:
    """El frontend lo llama al arrancar para saber si mostrar la app o el login."""
    return _out(user)


@router.post(
    "/change-password",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Cambiar mi contraseña",
)
async def change_password(
    request: Request,
    session: DbSession,
    user: CurrentUser,
    payload: PasswordChange,
) -> None:
    """Cambia la contraseña y cierra las demás sesiones.

    Pide la contraseña actual aunque ya estés logueado: si no, quien te agarra
    la computadora desbloqueada te cambia la contraseña y te deja afuera de tu
    propia cuenta.

    Cerrar las otras sesiones es la mitad que se olvida. Cambiar la contraseña
    suele ser la reacción a "creo que alguien entró", y si las sesiones que ese
    alguien abrió siguen vivas, el cambio no sirvió de nada.
    """
    if user.password_hash != UNUSABLE_PASSWORD and not verify_password(
        payload.current_password, user.password_hash
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="La contraseña actual no coincide.",
        )

    user.password_hash = hash_password(payload.new_password)

    current = getattr(request.state, "auth_session", None)
    await AuthSessionRepository(session).delete_for_user(
        user.id, except_id=current.id if current is not None else None
    )
    await session.commit()
