"""La lista, manejada desde afuera con un token personal.

Existe para el Atajo de Siri del iPhone: *"Oye Siri, agregar a Ahorrito"* → Siri
pregunta qué → se dicta el producto → el atajo hace un POST acá. Sirve igual para
cualquier otra cosa que pueda hacer una request con un header: un `curl`, un
widget, un bot.

Tres decisiones que definen este archivo:

* **Autentica por token, no por cookie.** El atajo corre en el sistema operativo
  del teléfono, no en un navegador: no tiene sesión, ni forma de pasar por un
  login, ni dónde guardar una cookie. El token se pega una vez y queda.

* **El token no vale para la cuenta entera, sólo para la lista.** Es un secreto
  que vive en texto plano adentro de un atajo, se sincroniza por iCloud y se
  comparte sin querer al compartir el atajo. Que lo peor que se pueda hacer con
  él sea anotar leche es el punto, no una limitación.

* **Todo devuelve una frase para decir en voz alta**, en el campo `dicho`. El
  atajo la pasa a "Hablar texto" y listo. Un JSON con `{"ok": true}` obligaría a
  armar la frase del lado del atajo, que es el peor lugar para tener lógica: no
  se versiona, no se prueba, y hay que editarlo a mano en cada teléfono.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import CurrentUser
from app.core.db import get_db
from app.db import tables as t
from app.db.repositories import ApiTokenRepository
from app.services import lista

log = logging.getLogger(__name__)

router = APIRouter(prefix="/voz", tags=["voz"])

DbSession = Annotated[AsyncSession, Depends(get_db)]

VOZ_PATHS = frozenset({"/api/v1/voz/agregar", "/api/v1/voz/lista", "/api/v1/voz/borrar"})
"""Lo que la puerta de acceso (`ACCESS_KEY`) tiene que dejar pasar.

Un Atajo de Siri no tiene cómo presentar la clave del sitio: no es un navegador
con cookie ni un script nuestro donde se pueda agregar un header más. Sin la
exención, el atajo recibe un 401 y lo único que se escucha es "no pude hacerlo",
sin ninguna pista de por qué.

Estar exento de la puerta no los deja abiertos: cada uno pide el token personal,
que son 256 bits, y con él no se puede hacer nada más que tocar la lista.
"""


class Dicho(BaseModel):
    """Lo que el atajo va a decir en voz alta."""

    dicho: str


class Producto(BaseModel):
    producto: str = Field(default="", max_length=120)
    """Vacío se acepta a propósito: el dictado a veces no entiende el audio del
    medio y manda una cadena vacía. `services/lista.py` lo contesta hablando
    —"no te entendí qué producto"— en vez de devolver un 422 que el atajo
    convertiría en un "no pude hacerlo" mudo."""


async def _usuario(
    session: DbSession,
    authorization: Annotated[str, Header()] = "",
) -> int:
    """Resuelve el `Authorization: Bearer <token>` a un `user_id`.

    Es la única puerta de este módulo. Devuelve el id y no la fila para que
    ningún endpoint pueda leer de ahí algo que no le corresponde.
    """
    esquema, _, token = authorization.partition(" ")
    if esquema.lower() != "bearer" or not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Falta el token.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    tokens = ApiTokenRepository(session)
    row = await tokens.resolve(token)
    if row is None:
        log.warning("Voz: token desconocido")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token inválido.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    await tokens.touch(row)
    await session.commit()
    return row.user_id


TokenUser = Annotated[int, Depends(_usuario)]


@router.post("/agregar", response_model=Dicho, summary="Agregar un producto por voz")
async def agregar(user_id: TokenUser, session: DbSession, payload: Producto) -> Dicho:
    return Dicho(dicho=await lista.agregar_producto(session, user_id, payload.producto))


@router.get("/lista", response_model=Dicho, summary="Leer la lista en voz alta")
async def leer(user_id: TokenUser, session: DbSession) -> Dicho:
    return Dicho(dicho=await lista.leer_lista(session, user_id))


@router.post("/borrar", response_model=Dicho, summary="Sacar un producto por voz")
async def borrar(user_id: TokenUser, session: DbSession, payload: Producto) -> Dicho:
    return Dicho(dicho=await lista.borrar_producto(session, user_id, payload.producto))


# ------------------------------------------------- administrar los tokens
#
# Esto sí va por cookie de sesión: se maneja desde la pantalla de configuración,
# en el navegador. Un token no puede emitir otro token —si pudiera, el "sólo
# sirve para la lista" duraría hasta que alguien lo usara para escalar.


class TokenOut(BaseModel):
    id: int
    name: str
    created_at: t.datetime
    last_used_at: t.datetime | None = None


class TokenNuevo(TokenOut):
    token: str
    """El valor en claro. **Se devuelve una sola vez, acá.** Después no hay forma
    de recuperarlo: en la base queda el hash."""


class NombreToken(BaseModel):
    name: str = Field(default="iPhone", max_length=60)


def _out(row: t.ApiToken) -> TokenOut:
    return TokenOut(
        id=row.id,
        name=row.name,
        # `as_aware` para que el JSON lleve el offset: SQLite devuelve el datetime
        # naive y un ISO sin zona lo interpreta el navegador como hora local, lo
        # que corre la fecha un día entero para quien lo creó cerca de medianoche.
        created_at=t.as_aware(row.created_at),
        last_used_at=t.as_aware(row.last_used_at) if row.last_used_at else None,
    )


@router.get("/tokens", response_model=list[TokenOut], summary="Mis tokens")
async def listar_tokens(user: CurrentUser, session: DbSession) -> list[TokenOut]:
    return [_out(row) for row in await ApiTokenRepository(session).for_user(user.id)]


@router.post(
    "/tokens",
    response_model=TokenNuevo,
    status_code=status.HTTP_201_CREATED,
    summary="Crear un token para el atajo",
)
async def crear_token(
    user: CurrentUser, session: DbSession, payload: NombreToken
) -> TokenNuevo:
    """Emite un token nuevo y lo devuelve en claro, por única vez."""
    tokens = ApiTokenRepository(session)
    token = await tokens.issue(user.id, name=payload.name)
    await session.commit()

    row = (await tokens.for_user(user.id))[0]
    log.info("Voz: token emitido para el usuario %d", user.id)
    return TokenNuevo(**_out(row).model_dump(), token=token)


@router.delete(
    "/tokens/{token_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revocar un token",
)
async def revocar_token(
    user: CurrentUser, session: DbSession, token_id: int
) -> Response:
    """204 exista o no: el resultado que se pidió —que ese token no sirva más— se
    cumple igual, y un 404 sólo haría que la pantalla muestre un error por tocar
    dos veces."""
    await ApiTokenRepository(session).delete(user.id, token_id)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
