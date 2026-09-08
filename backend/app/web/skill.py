"""`POST /alexa/skill`: el único endpoint que le damos a Amazon.

Es la puerta más expuesta del deploy —pública, sin cookie, sin clave de acceso,
y escribe en la lista de compras de un usuario— así que el orden de este archivo
es el orden de la verificación, y no hay forma de llegar a la lógica de negocio
sin pasar por los cuatro pasos de arriba.

Dos cosas contraintuitivas que valen el comentario:

* **Se lee el cuerpo crudo antes de parsearlo.** La firma es sobre esos bytes
  exactos. Si se dejara que FastAPI parseara el JSON y después se re-serializara
  para verificar, un espacio de diferencia rompería todo, y el error diría "firma
  inválida" en vez de "lo serializaste distinto".
* **Casi todo contesta 200.** Una vez que el request está verificado, cualquier
  problema se le cuenta al usuario hablando. Un 500 le hace decir a Alexa "hubo
  un problema con la skill solicitada", que no le sirve a nadie: el usuario no se
  entera de qué pasó y nosotros tampoco, porque el log queda del lado de Amazon.
  Los 400 de acá son todos anteriores a la verificación.
"""

from __future__ import annotations

import json
import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_db
from app.db.repositories import AlexaSkillTokenRepository
from app.services.alexa import skill
from app.services.alexa.signature import (
    CERT_CHAIN_HEADER,
    SIGNATURE_HEADER,
    SignatureError,
    check_timestamp,
    verify,
)

log = logging.getLogger(__name__)

router = APIRouter(include_in_schema=False)

SKILL_PATHS = frozenset({"/alexa/skill"})
"""Lo que la puerta de acceso tiene que dejar pasar.

Amazon no tiene cómo presentar la `ACCESS_KEY`: no es un navegador con cookie ni
un script nuestro con el header. Sin esta exención el skill contesta 401 a todo
y el síntoma es mudo —Alexa dice "hubo un problema" y no hay nada en los logs
que lo explique—. Vive acá, junto a la ruta, por lo mismo que `PRIVACY_PATHS`.

La exención no lo deja abierto: lo que protege este endpoint es la firma de
Amazon, que es una defensa más fuerte que una clave compartida en una cookie.
"""

DbSession = Annotated[AsyncSession, Depends(get_db)]


def is_configured() -> bool:
    """Si el skill está encendido en este deploy.

    Solo mira `ALEXA_SKILL_ID` porque es lo único sin lo cual la verificación es
    imposible: sin ese valor no hay contra qué comparar el `applicationId`, y un
    endpoint que acepta requests firmados por Amazon sin mirar de qué skill vienen
    lo puede usar cualquier developer del mundo para escribir en estas listas.
    """
    return bool(settings.ALEXA_SKILL_ID)


@router.post("/alexa/skill")
async def alexa_skill(request: Request, session: DbSession) -> Response:
    if not is_configured():
        # 404 y no 503: si el skill no está configurado, esta URL no existe. No
        # hay motivo para confirmarle a nadie que el endpoint está ahí, apagado.
        return _json({"detail": "No encontrado."}, status.HTTP_404_NOT_FOUND)

    raw = await request.body()

    try:
        await verify(
            raw,
            signature=request.headers.get(SIGNATURE_HEADER),
            cert_url=request.headers.get(CERT_CHAIN_HEADER),
        )
    except SignatureError as exc:
        # El motivo va al log y no a la respuesta: quien esté probando firmas no
        # tiene por qué recibir el detalle de cuál de los chequeos lo frenó.
        log.warning("Alexa: request rechazado (%s)", exc)
        return _json({"detail": "Firma inválida."}, status.HTTP_400_BAD_REQUEST)

    try:
        envelope = json.loads(raw)
        if not isinstance(envelope, dict):
            raise ValueError("el sobre no es un objeto")
    except ValueError as exc:
        log.warning("Alexa: cuerpo ilegible (%s)", exc)
        return _json({"detail": "Cuerpo inválido."}, status.HTTP_400_BAD_REQUEST)

    body = envelope.get("request") or {}
    system = (envelope.get("context") or {}).get("System") or {}

    try:
        check_timestamp(body.get("timestamp"))
    except SignatureError as exc:
        log.warning("Alexa: %s", exc)
        return _json({"detail": "Request vencido."}, status.HTTP_400_BAD_REQUEST)

    app_id = (system.get("application") or {}).get("applicationId")
    if app_id != settings.ALEXA_SKILL_ID:
        # Firma válida pero de otro skill. La firma de Amazon es la misma para
        # todos los skills, así que este chequeo es lo único que impide que el
        # skill de un tercero escriba en las listas de nuestros usuarios.
        log.warning("Alexa: applicationId ajeno")
        return _json({"detail": "Skill desconocido."}, status.HTTP_400_BAD_REQUEST)

    token = (system.get("user") or {}).get("accessToken")
    if not token:
        return _json(skill.link_account())

    tokens = AlexaSkillTokenRepository(session)
    row = await tokens.by_access_token(token)
    if row is None:
        # Vencido o revocado desde la app. Se contesta lo mismo que si no
        # hubiera token: para el usuario los dos casos son "hay que vincular".
        return _json(skill.link_account())

    user_id = row.user_id
    await tokens.touch(row)
    await session.commit()

    try:
        return _json(await skill.handle(session, user_id, body))
    except Exception:
        # Deliberadamente ancho. Cualquier cosa que se rompa acá adentro tiene
        # que salir como una frase y no como un 500: el usuario está parado en la
        # cocina esperando que le confirmen que anotó la leche.
        log.exception("Alexa: falló el intent del usuario %d", user_id)
        await session.rollback()
        return _json(skill.speak("Se me complicó. Probá de nuevo en un ratito."))


def _json(payload: dict[str, Any], code: int = status.HTTP_200_OK) -> Response:
    """`Response` crudo en vez de `JSONResponse` de FastAPI.

    Da igual funcionalmente; lo que evita es que alguien le ponga un
    `response_model` a la ruta. El sobre de Alexa tiene una forma que la define
    Amazon y que cambia con cada tipo de directiva: validarlo contra un modelo
    nuestro solo agregaría una forma de romper la respuesta sin darnos cuenta.
    """
    return Response(
        content=json.dumps(payload, ensure_ascii=False),
        media_type="application/json",
        status_code=code,
    )
