"""Estado del vínculo con Alexa, para la pantalla de configuración.

Acá vive lo que la SPA sí puede pedir por `fetch`: si hay vínculo y desde cuándo,
y cómo cortarlo. El flujo de autorización en sí no puede estar acá —tiene que ser
una navegación de nivel superior hacia Amazon, un `fetch` a eso muere en CORS— y
vive en `web/alexa.py`, en la raíz.

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
from app.db.repositories import AlexaLinkRepository
from app.services.alexa import lwa

router = APIRouter(prefix="/alexa", tags=["alexa"])

DbSession = Annotated[AsyncSession, Depends(get_db)]


class AlexaStatus(BaseModel):
    """Lo que la pantalla de configuración necesita saber.

    `configured` es distinto de `linked` y hace falta para que el botón sea
    honesto: sin secrets en el deploy, ofrecer "Vincular cuenta de Alexa" manda
    al usuario a un callejón. Con esto el frontend puede decir que la función no
    está disponible en vez de hacerlo probar.
    """

    configured: bool
    linked: bool
    linked_at: datetime | None = None
    scope: str | None = None


@router.get("/status", response_model=AlexaStatus, summary="Estado del vínculo con Alexa")
async def status_(user: CurrentUser, session: DbSession) -> AlexaStatus:
    row = await AlexaLinkRepository(session, user.id).get()
    if row is None:
        return AlexaStatus(configured=lwa.is_configured(), linked=False)
    return AlexaStatus(
        configured=lwa.is_configured(),
        linked=True,
        # `as_aware` para que el JSON lleve el offset: SQLite devuelve el
        # datetime naive, y un ISO sin zona lo interpreta el navegador como hora
        # local. Sobre una fecha que se muestra sola, eso la corre un día entero
        # para quien vinculó cerca de la medianoche.
        linked_at=t.as_aware(row.linked_at),
        scope=row.scope,
    )


@router.delete(
    "/link",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Desvincular la cuenta de Alexa",
)
async def unlink(user: CurrentUser, session: DbSession) -> Response:
    """Borra el vínculo de este lado.

    Existe para no obligar al usuario a ir a amazon.com a cortar algo que empezó
    acá. **No revoca el permiso del lado de Amazon**: el vínculo sigue figurando
    en su cuenta hasta que lo saque de ahí. Lo que sí garantiza es lo que importa
    —que esta app deje de tener con qué entrar—, porque sin la fila no queda
    ningún token guardado.

    204 tanto si había vínculo como si no: el resultado que el usuario pidió
    —que no haya vínculo— se cumplió igual, y un 404 solo invitaría al frontend
    a mostrar un error por apretar dos veces.
    """
    await AlexaLinkRepository(session, user.id).delete()
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
