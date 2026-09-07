"""Endpoints de perfil, domicilios y listas de compras.

Todo lo que antes vivía en el `localStorage` del navegador y por eso era por
dispositivo: el código postal que resuelve la sucursal, el canal que decide qué
promos aplican, y la lista de compras.

Todo cuelga del usuario autenticado: los repositorios reciben `user.id` en el
constructor, así que un domicilio ajeno no aparece en la lista ni se puede tocar
por id —`by_id` devuelve None si el dueño no coincide, y el endpoint contesta el
mismo 404 que si no existiera—.
"""

from __future__ import annotations

import re
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import CurrentUser
from app.core.db import get_db
from app.db.repositories import (
    AddressRepository,
    ShoppingListRepository,
    UserProfileRepository,
)
from app.models.payment import Channel

router = APIRouter()

DbSession = Annotated[AsyncSession, Depends(get_db)]


# ---------------------------------------------------------------------- perfil


_DNI_RE = re.compile(r"^[\d.\-]{6,20}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class ProfileIn(BaseModel):
    """Los datos del usuario.

    El DNI y el email se validan solo como formato: son datos que escribís vos
    para vos, y no hay ningún registro contra el cual contrastarlos. La
    validación existe para atajar el dedazo, no para decidir si sos quien decís.
    """

    postal_code: str | None = Field(None, max_length=16, examples=["1425"])
    channel: Channel = Channel.IN_STORE
    sales_channel: int | None = Field(None, ge=1)
    theme: str = Field("system", pattern="^(system|light|dark)$")

    full_name: str | None = Field(None, max_length=200, examples=["Ejemplo Ejemplito"])
    dni: str | None = Field(None, max_length=20, examples=["30123456"])
    email: str | None = Field(None, max_length=254, examples=["ejemplo@ejemplo.com"])
    phone: str | None = Field(None, max_length=40, examples=["+54 9 11 5555-5555"])

    @field_validator("postal_code", "full_name", "dni", "email", "phone", mode="after")
    @classmethod
    def _blank_is_none(cls, value: str | None) -> str | None:
        """Un campo vaciado en la UI llega como `""`, y eso es borrarlo.

        Guardarlo como cadena vacía haría que `full_name or "—"` muestre el dato
        vacío en vez del placeholder, y que borrar un campo no se distinga de
        nunca haberlo cargado.
        """
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None

    @field_validator("dni", mode="after")
    @classmethod
    def _check_dni(cls, value: str | None) -> str | None:
        if value is not None and not _DNI_RE.match(value):
            raise ValueError("El DNI solo puede tener números, puntos y guiones.")
        return value

    @field_validator("email", mode="after")
    @classmethod
    def _check_email(cls, value: str | None) -> str | None:
        if value is not None and not _EMAIL_RE.match(value):
            raise ValueError("Ese email no tiene forma de email.")
        return value


class ProfileOut(ProfileIn):
    pass


def _profile_out(row) -> ProfileOut:
    return ProfileOut(
        postal_code=row.postal_code,
        channel=row.channel,
        sales_channel=row.sales_channel,
        theme=row.theme,
        full_name=row.full_name,
        dni=row.dni,
        email=row.email,
        phone=row.phone,
    )


@router.get(
    "/profile",
    response_model=ProfileOut,
    summary="Mis datos y preferencias de compra",
    tags=["perfil"],
)
async def get_profile(session: DbSession, user: CurrentUser) -> ProfileOut:
    """El perfil, creado con defaults si es la primera vez.

    Nunca devuelve 404: "no hay perfil" y "hay uno vacío" son el mismo estado —
    una app recién instalada— y distinguirlos solo le daría al frontend una rama
    más que atender.
    """
    row = await UserProfileRepository(session, user.id).get_or_create()
    await session.commit()
    return _profile_out(row)


@router.put(
    "/profile",
    response_model=ProfileOut,
    summary="Guardar mis datos",
    tags=["perfil"],
)
async def save_profile(
    session: DbSession, user: CurrentUser, payload: ProfileIn
) -> ProfileOut:
    repo = UserProfileRepository(session, user.id)
    row = await repo.get_or_create()
    await repo.update(
        row,
        {
            "postal_code": payload.postal_code,
            "channel": str(payload.channel),
            "sales_channel": payload.sales_channel,
            "theme": payload.theme,
            "full_name": payload.full_name,
            "dni": payload.dni,
            "email": payload.email,
            "phone": payload.phone,
        },
    )
    await session.commit()
    return _profile_out(row)


# ------------------------------------------------------------------ domicilios


class AddressIn(BaseModel):
    label: str = Field(..., min_length=1, max_length=120, examples=["Casa"])
    street: str | None = Field(None, max_length=200, examples=["Av. Corrientes"])
    number: str | None = Field(None, max_length=20, examples=["1234"])
    floor: str | None = Field(None, max_length=20, examples=["3"])
    apartment: str | None = Field(None, max_length=20, examples=["B"])
    city: str | None = Field(None, max_length=120, examples=["CABA"])
    province: str | None = Field(None, max_length=120, examples=["Buenos Aires"])
    postal_code: str | None = Field(None, max_length=16, examples=["1425"])
    notes: str | None = Field(None, max_length=500, examples=["Timbre roto"])
    is_default: bool = False

    latitude: float | None = Field(None, ge=-90, le=90, examples=[-34.6056])
    longitude: float | None = Field(None, ge=-180, le=180, examples=[-58.4135])
    geo_source: str | None = Field(
        None,
        description="`geocoded` si salió de buscar la dirección, `manual` si la marcaste en el mapa.",
        examples=["geocoded"],
    )
    geo_label: str | None = Field(
        None,
        max_length=300,
        description="Cómo entendió la dirección el geocodificador.",
        examples=["GALLO 250, Comuna 3, Ciudad Autónoma de Buenos Aires"],
    )


class AddressUpdate(BaseModel):
    label: str | None = Field(None, min_length=1, max_length=120)
    street: str | None = Field(None, max_length=200)
    number: str | None = Field(None, max_length=20)
    floor: str | None = Field(None, max_length=20)
    apartment: str | None = Field(None, max_length=20)
    city: str | None = Field(None, max_length=120)
    province: str | None = Field(None, max_length=120)
    postal_code: str | None = Field(None, max_length=16)
    notes: str | None = Field(None, max_length=500)
    is_default: bool | None = None

    latitude: float | None = Field(None, ge=-90, le=90)
    longitude: float | None = Field(None, ge=-180, le=180)
    geo_source: str | None = None
    geo_label: str | None = Field(None, max_length=300)


class AddressOut(AddressIn):
    id: int


def _address_out(row) -> AddressOut:
    return AddressOut(
        id=row.id,
        label=row.label,
        street=row.street,
        number=row.number,
        floor=row.floor,
        apartment=row.apartment,
        city=row.city,
        province=row.province,
        postal_code=row.postal_code,
        notes=row.notes,
        is_default=row.is_default,
        latitude=row.latitude,
        longitude=row.longitude,
        geo_source=row.geo_source,
        geo_label=row.geo_label,
    )


@router.get(
    "/addresses",
    response_model=list[AddressOut],
    summary="Mis domicilios",
    tags=["perfil"],
)
async def list_addresses(session: DbSession, user: CurrentUser) -> list[AddressOut]:
    rows = await AddressRepository(session, user.id).list()
    return [_address_out(row) for row in rows]


@router.post(
    "/addresses",
    response_model=AddressOut,
    status_code=status.HTTP_201_CREATED,
    summary="Guardar un domicilio",
    tags=["perfil"],
)
async def create_address(
    session: DbSession, user: CurrentUser, payload: AddressIn
) -> AddressOut:
    repo = AddressRepository(session, user.id)
    try:
        row = await repo.create(payload.model_dump())
        if payload.is_default:
            await repo.clear_default(except_id=row.id)
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Ya tenés un domicilio llamado «{payload.label}».",
        ) from exc
    return _address_out(row)


@router.patch(
    "/addresses/{address_id}",
    response_model=AddressOut,
    summary="Editar un domicilio",
    tags=["perfil"],
)
async def update_address(
    session: DbSession,
    user: CurrentUser,
    address_id: int,
    payload: AddressUpdate,
) -> AddressOut:
    repo = AddressRepository(session, user.id)
    row = await repo.by_id(address_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Domicilio inexistente")

    changes = payload.model_dump(exclude_unset=True)
    try:
        await repo.update(row, changes)
        if changes.get("is_default"):
            await repo.clear_default(except_id=row.id)
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Ya tenés un domicilio llamado «{payload.label}».",
        ) from exc
    return _address_out(row)


@router.delete(
    "/addresses/{address_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Borrar un domicilio",
    tags=["perfil"],
)
async def delete_address(session: DbSession, user: CurrentUser, address_id: int) -> None:
    repo = AddressRepository(session, user.id)
    row = await repo.by_id(address_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Domicilio inexistente")
    await repo.delete(row)
    await session.commit()


# ------------------------------------------------------------ listas de compras


class ListLine(BaseModel):
    """Una línea, con los mismos límites que acepta `BasketLine`.

    Si acá entrara algo que la comparación rechaza, la lista se guardaría bien y
    el error aparecería después, al comparar, disfrazado de "el súper no
    respondió".
    """

    query: str = Field(..., min_length=1, max_length=120, examples=["leche entera 1L"])
    quantity: int = Field(1, ge=1, le=99)
    ean: str | None = Field(None, max_length=14, examples=["7790895000997"])
    pinned_label: str | None = Field(None, max_length=400)


class ListIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=120, examples=["Semanal"])
    lines: list[ListLine] = Field(default_factory=list, max_length=50)


class ListLinesIn(BaseModel):
    """Lo que manda el frontend al guardar: el estado completo de la lista.

    Reemplazo total y no diff — el techo es 50 líneas y la punta que sabe el
    orden es la UI.
    """

    lines: list[ListLine] = Field(default_factory=list, max_length=50)
    name: str | None = Field(None, min_length=1, max_length=120)


class ListOut(BaseModel):
    id: int
    name: str
    lines: list[ListLine] = Field(default_factory=list)


def _list_out(row) -> ListOut:
    return ListOut(
        id=row.id,
        name=row.name,
        lines=[
            ListLine(
                query=line.query,
                quantity=line.quantity,
                ean=line.ean,
                pinned_label=line.pinned_label,
            )
            for line in row.lines
        ],
    )


@router.get(
    "/shopping-lists",
    response_model=list[ListOut],
    summary="Mis listas de compras",
    tags=["lista"],
)
async def list_shopping_lists(session: DbSession, user: CurrentUser) -> list[ListOut]:
    rows = await ShoppingListRepository(session, user.id).list()
    return [_list_out(row) for row in rows]


@router.get(
    "/shopping-lists/default",
    response_model=ListOut,
    summary="La lista con la que trabaja la pantalla principal",
    tags=["lista"],
)
async def get_default_list(session: DbSession, user: CurrentUser) -> ListOut:
    """La más reciente, o una nueva vacía si todavía no hay ninguna.

    Va antes que `/shopping-lists/{list_id}` a propósito: FastAPI resuelve por
    orden de declaración, y al revés `default` entraría como id y daría un 422.
    """
    row = await ShoppingListRepository(session, user.id).default_list()
    await session.commit()
    return _list_out(row)


@router.post(
    "/shopping-lists",
    response_model=ListOut,
    status_code=status.HTTP_201_CREATED,
    summary="Crear una lista",
    tags=["lista"],
)
async def create_shopping_list(
    session: DbSession, user: CurrentUser, payload: ListIn
) -> ListOut:
    repo = ShoppingListRepository(session, user.id)
    try:
        row = await repo.create(payload.name)
        await repo.replace_lines(row, [line.model_dump() for line in payload.lines])
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Ya tenés una lista llamada «{payload.name}».",
        ) from exc
    return _list_out(row)


@router.get(
    "/shopping-lists/{list_id}",
    response_model=ListOut,
    summary="Una lista",
    tags=["lista"],
)
async def get_shopping_list(session: DbSession, user: CurrentUser, list_id: int) -> ListOut:
    row = await ShoppingListRepository(session, user.id).by_id(list_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Lista inexistente")
    return _list_out(row)


@router.put(
    "/shopping-lists/{list_id}",
    response_model=ListOut,
    summary="Guardar la lista entera",
    tags=["lista"],
)
async def save_shopping_list(
    session: DbSession,
    user: CurrentUser,
    list_id: int,
    payload: ListLinesIn,
) -> ListOut:
    repo = ShoppingListRepository(session, user.id)
    row = await repo.by_id(list_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Lista inexistente")

    try:
        if payload.name is not None:
            row.name = payload.name
        await repo.replace_lines(row, [line.model_dump() for line in payload.lines])
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Ya tenés una lista llamada «{payload.name}».",
        ) from exc
    return _list_out(row)


@router.delete(
    "/shopping-lists/{list_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Borrar una lista",
    tags=["lista"],
)
async def delete_shopping_list(session: DbSession, user: CurrentUser, list_id: int) -> None:
    repo = ShoppingListRepository(session, user.id)
    row = await repo.by_id(list_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Lista inexistente")
    await repo.delete(row)
    await session.commit()
