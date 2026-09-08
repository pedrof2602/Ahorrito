"""Estado del vínculo con Alexa, para la pantalla de configuración.

Acá vive lo que la SPA sí puede pedir por `fetch`: si el skill está vinculado y
desde cuándo, y cómo cortarlo.

**El vínculo ya no empieza en esta app.** Empieza en la app de Alexa, cuando el
usuario activa el skill y aprieta "Vincular cuenta": de ahí sale una navegación
a `/oauth/alexa/authorize`, que es un formulario de login y no un endpoint de
API. Por eso esta pantalla explica cómo hacerlo en vez de ofrecer un botón —no
hay nada que podamos iniciar desde acá— y por eso lo único accionable que queda
es desvincular.

**Ningún endpoint de este archivo devuelve un token**, ni entero ni recortado.
Lo que el frontend necesita para dibujar la pantalla es un booleano y una fecha.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import CurrentUser
from app.core.db import get_db
from app.db import tables as t
from app.db.repositories import AlexaSkillTokenRepository
from app.web import oauth_alexa, skill

router = APIRouter(prefix="/alexa", tags=["alexa"])

DbSession = Annotated[AsyncSession, Depends(get_db)]


class AlexaStatus(BaseModel):
    """Lo que la pantalla de configuración necesita saber.

    `configured` es distinto de `linked` y hace falta para que la pantalla sea
    honesta: sin los secrets en el deploy, explicarle al usuario cómo vincular lo
    manda a un callejón. Con esto el frontend puede decir que la función no está
    disponible en vez de hacerlo probar.
    """

    configured: bool
    linked: bool
    linked_at: datetime | None = None
    last_used_at: datetime | None = None
    devices: int = 0
    """Cuántos vínculos hay. Puede ser más de uno —la casa y lo de los padres— y
    la pantalla lo menciona solo cuando pasa de uno, para no explicar de entrada
    algo que a casi nadie le va a pasar."""


@router.get("/status", response_model=AlexaStatus, summary="Estado del vínculo con Alexa")
async def status_(user: CurrentUser, session: DbSession) -> AlexaStatus:
    configured = skill.is_configured() and oauth_alexa.is_configured()

    rows = await AlexaSkillTokenRepository(session).for_user(user.id)
    if not rows:
        return AlexaStatus(configured=configured, linked=False)

    # El más reciente primero: `for_user` ordena por `created_at` descendente.
    newest = rows[0]
    usados = [r.last_used_at for r in rows if r.last_used_at is not None]

    return AlexaStatus(
        configured=configured,
        linked=True,
        # `as_aware` para que el JSON lleve el offset: SQLite devuelve el
        # datetime naive, y un ISO sin zona lo interpreta el navegador como hora
        # local. Sobre una fecha que se muestra sola, eso la corre un día entero
        # para quien vinculó cerca de la medianoche.
        linked_at=t.as_aware(newest.created_at),
        last_used_at=max(t.as_aware(u) for u in usados) if usados else None,
        devices=len(rows),
    )


@router.delete(
    "/link",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Desvincular la cuenta de Alexa",
)
async def unlink(user: CurrentUser, session: DbSession) -> Response:
    """Corta **todos** los vínculos de este usuario.

    Todos y no uno: desde la app el usuario ve "Alexa" como una sola cosa, y
    ofrecerle desvincular la tercera de tres sería pedirle que distinga entre
    filas que nunca vio.

    Borrar la fila alcanza para que Alexa deje de entrar: sin ella, el
    `access_token` que tiene guardado no resuelve a ningún usuario y el próximo
    `refresh_token` devuelve `invalid_grant`, que es lo que le hace pedir al
    usuario que vincule de nuevo. **No borra el skill de su cuenta de Amazon**:
    eso lo saca él desde la app de Alexa, y hasta que lo haga va a seguir viendo
    el skill ahí, pidiéndole vincular.

    204 tanto si había vínculo como si no: el resultado que el usuario pidió
    —que no haya vínculo— se cumplió igual, y un 404 solo invitaría al frontend
    a mostrar un error por apretar dos veces.
    """
    await AlexaSkillTokenRepository(session).delete_for_user(user.id)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
