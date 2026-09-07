"""Dónde queda cada cadena.

Va aparte de la comparación por dos motivos. Uno semántico: `ChainTotal.store`
significa *la sucursal cuyos precios son estos*, y meterle una sucursal cercana
cuando el precio es nacional rompería esa definición y la honestidad de
`price_scope`. Uno práctico: las ubicaciones son datos casi estáticos, así que se
piden una vez al abrir el mapa en lugar de viajar en cada comparación.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.auth import CurrentUser
from app.core.db import get_db
from app.db.repositories import ChainRepository, CustomStoreRepository
from app.services.http import ProviderError
from app.services.locations.georef import GeorefGeocoder
from app.services.locations.lookup import nearest_by_chain, resolve_center
from app.services.providers.registry import ProviderRegistry

router = APIRouter()

DbSession = Annotated[AsyncSession, Depends(get_db)]


def get_registry(request: Request) -> ProviderRegistry:
    return request.app.state.registry


Registry = Annotated[ProviderRegistry, Depends(get_registry)]


class StoreLocation(BaseModel):
    chain_slug: str = Field(..., examples=["carrefour-ar"])
    external_id: str = Field(..., examples=["carrefourar0026"])
    name: str = Field(..., examples=["Hiper Warnes"])
    latitude: float
    longitude: float
    distance_km: float | None = Field(
        None, description="Al centro. Null si no se pudo ubicar al usuario."
    )
    location_source: str | None = Field(
        None,
        description=(
            "`api` si la coordenada la publicó la cadena, `geocoded` si salió de "
            "geocodificar la dirección — en ese caso es aproximada."
        ),
        examples=["api"],
    )
    address: str | None = None
    city: str | None = None
    custom_store_id: int | None = Field(
        None, description="Id en /custom-stores si la cargaste vos."
    )


class StoreLocationsResponse(BaseModel):
    """Las sucursales para el mapa, y dónde centrarlo."""

    postal_code: str
    center_latitude: float | None = None
    center_longitude: float | None = None
    center_source: str | None = Field(
        None,
        description=(
            "Desde dónde se midieron las distancias: `explicit` (unas "
            "coordenadas puntuales), `address` (tu domicilio ubicado), "
            "`postal_code` (el promedio de las sucursales de tu CP) o "
            "`geocoded`. Cambia cuánto vale «la más cercana»."
        ),
        examples=["address"],
    )
    center_label: str | None = Field(
        None, description="Qué domicilio se usó como centro, si fue uno."
    )
    stores: list[StoreLocation] = Field(default_factory=list)
    note: str | None = Field(
        None, description="Por qué la respuesta viene incompleta, si viene."
    )


@router.get(
    "/store-locations",
    response_model=StoreLocationsResponse,
    summary="Sucursal más cercana de cada cadena",
    tags=["mapa"],
)
async def store_locations(
    session: DbSession,
    user: CurrentUser,
    postal_code: Annotated[str | None, Query(description="Código postal")] = None,
    latitude: Annotated[
        float | None,
        Query(ge=-90, le=90, description="Medir las distancias desde acá."),
    ] = None,
    longitude: Annotated[float | None, Query(ge=-180, le=180)] = None,
) -> StoreLocationsResponse:
    """La sucursal ubicada más próxima de cada cadena.

    **No devuelve precios.** Los totales los calcula la comparación y el mapa los
    toma de ahí; si los recalculara acá, la tabla y el mapa podrían mostrar
    números distintos para la misma canasta.

    Una cadena sin sucursales ubicadas simplemente no aparece. No es un error:
    Disco no publica sus sucursales, igual que no publica regiones, y devolverlo
    como fallo obligaría al frontend a distinguir "se rompió" de "no hay", que
    acá son cosas distintas.

    De dónde sale el centro lo decide `resolve_center`, y el frontend no tiene
    que saberlo: si hay un domicilio ubicado se usa ese, y si no se cae al código
    postal como siempre. `latitude`/`longitude` son para el caso puntual de
    querer medir desde otro lado sin tocar el perfil.
    """
    code = postal_code or settings.DEFAULT_POSTAL_CODE
    explicit = (
        (latitude, longitude) if latitude is not None and longitude is not None else None
    )
    center = await resolve_center(session, code, user_id=user.id, explicit=explicit)
    stores = await nearest_by_chain(
        session, center.point if center else None, user_id=user.id
    )

    note = None
    if not stores:
        note = (
            "Todavía no hay sucursales ubicadas. Corré "
            "scripts/refresh_store_locations.py para cargarlas, o cargá a mano "
            "las que tenés cerca."
        )
    elif center is None:
        note = (
            f"No se pudo ubicar el código postal {code}: los markers se muestran "
            "igual, pero no están ordenados por cercanía. Cargá tu dirección en "
            "«Mis domicilios» para ordenarlos por cercanía real."
        )

    return StoreLocationsResponse(
        postal_code=code,
        center_latitude=center.latitude if center else None,
        center_longitude=center.longitude if center else None,
        center_source=center.source if center else None,
        center_label=center.label if center else None,
        stores=[StoreLocation(**asdict(store)) for store in stores],
        note=note,
    )


# --- geocodificación ---------------------------------------------------------


class GeocodeCandidate(BaseModel):
    label: str = Field(
        ..., examples=["GALLO 250, Comuna 3, Ciudad Autónoma de Buenos Aires"]
    )
    latitude: float
    longitude: float
    city: str | None = None
    province: str | None = None


class GeocodeResponse(BaseModel):
    candidates: list[GeocodeCandidate] = Field(default_factory=list)
    note: str | None = None


@router.get(
    "/geocode",
    response_model=GeocodeResponse,
    summary="Buscar una dirección",
    tags=["mapa"],
)
async def geocode(
    user: CurrentUser,
    address: Annotated[
        str, Query(min_length=3, max_length=200, description="Calle y altura")
    ],
    city: Annotated[
        str | None, Query(max_length=120, description="Partido o comuna")
    ] = None,
    province: Annotated[str | None, Query(max_length=120)] = None,
) -> GeocodeResponse:
    """Las direcciones que coinciden, para elegir una.

    **Devuelve varias a propósito y no elige por vos.** «Av. Belgrano 950» existe
    en Tres Arroyos, en Comuna 1 y en Saladillo, y quedarse con la primera es
    exactamente lo que pone un marker a trescientos kilómetros con total
    seguridad. Quien está cargando su dirección sabe cuál era; el servidor no.
    """
    try:
        async with GeorefGeocoder() as geocoder:
            found = await geocoder.search(address, city=city, province=province)
    except ProviderError as exc:
        # 502 y no 500: el que falló es un servicio de afuera, y el mensaje lo
        # dice para que se pueda intentar la salida manual en vez de suponer que
        # la app se rompió.
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            "No se pudo consultar el buscador de direcciones "
            f"(apis.datos.gob.ar): {exc}. Podés marcar el punto en el mapa.",
        ) from exc

    note = None
    if not found:
        note = (
            "No se encontró esa dirección. Probá sin el piso ni el "
            "departamento, o marcá el punto en el mapa."
        )
    return GeocodeResponse(
        candidates=[GeocodeCandidate(**asdict(c)) for c in found], note=note
    )


# --- sucursales cargadas a mano ----------------------------------------------


class CustomStoreIn(BaseModel):
    chain_slug: str = Field(..., max_length=64, examples=["coto-ar"])
    name: str = Field(..., min_length=1, max_length=200, examples=["Coto de casa"])
    latitude: float = Field(..., ge=-90, le=90, examples=[-34.6056])
    longitude: float = Field(..., ge=-180, le=180, examples=[-58.4135])
    address: str | None = Field(None, max_length=300, examples=["Gallo 250"])
    city: str | None = Field(None, max_length=120)
    province: str | None = Field(None, max_length=120)
    postal_code: str | None = Field(None, max_length=16)
    notes: str | None = Field(None, max_length=500)

    @model_validator(mode="after")
    def _not_null_island(self) -> CustomStoreIn:
        """(0, 0) queda en el Golfo de Guinea.

        Es lo que sale de un formulario que no se completó, no una sucursal, y
        dejarlo pasar rompe el encuadre del mapa: `fitBounds` estira el marco
        hasta África y las sucursales reales quedan en un punto.
        """
        if self.latitude == 0 and self.longitude == 0:
            raise ValueError("Ubicá la sucursal antes de guardarla.")
        return self


class CustomStoreUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=200)
    latitude: float | None = Field(None, ge=-90, le=90)
    longitude: float | None = Field(None, ge=-180, le=180)
    address: str | None = Field(None, max_length=300)
    city: str | None = Field(None, max_length=120)
    province: str | None = Field(None, max_length=120)
    postal_code: str | None = Field(None, max_length=16)
    notes: str | None = Field(None, max_length=500)


class CustomStoreOut(CustomStoreIn):
    id: int
    chain_name: str = Field(..., examples=["Coto"])


def _custom_out(row, chain) -> CustomStoreOut:
    # La cadena se pasa desde afuera y no se lee por `row.chain`: la relación es
    # lazy y con `AsyncSession` eso no es una consulta de más, es `MissingGreenlet`.
    return CustomStoreOut(
        id=row.id,
        chain_slug=chain.slug,
        chain_name=chain.display_name,
        name=row.name,
        latitude=row.latitude,
        longitude=row.longitude,
        address=row.address,
        city=row.city,
        province=row.province,
        postal_code=row.postal_code,
        notes=row.notes,
    )


async def _comparable_chain(session: AsyncSession, registry: ProviderRegistry, slug: str):
    """La fila de la cadena, si es una que la app sabe cotizar.

    El filtro por el registry es el que sostiene la promesa del mapa: cada marker
    muestra un total. Aceptar una cadena sin proveedor dejaría markers sin precio
    al lado de markers con precio, y dos cosas que se ven igual y significan
    distinto es peor que la ausencia del marker.
    """
    known = {provider.chain.slug for provider in registry.all()}
    if slug not in known:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"«{slug}» no es una cadena que la app compare. Se puede elegir entre: "
            + ", ".join(sorted(known))
            + ".",
        )
    chain = await ChainRepository(session).by_slug(slug)
    if chain is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Todavía no hay datos de «{slug}». Hacé una comparación primero.",
        )
    return chain


@router.get(
    "/custom-stores",
    response_model=list[CustomStoreOut],
    summary="Mis sucursales",
    tags=["mapa"],
)
async def list_custom_stores(
    session: DbSession, user: CurrentUser
) -> list[CustomStoreOut]:
    rows = await CustomStoreRepository(session, user.id).list()
    chains = {c.id: c for c in await ChainRepository(session).all()}
    return [_custom_out(row, chains[row.chain_id]) for row in rows if row.chain_id in chains]


@router.post(
    "/custom-stores",
    response_model=CustomStoreOut,
    status_code=status.HTTP_201_CREATED,
    summary="Cargar una sucursal a mano",
    tags=["mapa"],
)
async def create_custom_store(
    session: DbSession, user: CurrentUser, registry: Registry, payload: CustomStoreIn
) -> CustomStoreOut:
    """Agrega un súper que tenés cerca.

    Sirve para dos cosas concretas: cargar una sucursal que la cadena no publica
    —Coto publica direcciones en texto y geocodificarlas le erra seguido— y
    corregir una que quedó mal ubicada. Por eso una sucursal cargada acá le gana
    a la automática de la misma cadena a la hora de elegir la más cercana.

    **No cambia ningún precio.** El total que muestra el marker sigue siendo el
    de la cadena, calculado como siempre.
    """
    chain = await _comparable_chain(session, registry, payload.chain_slug)
    # El nombre se copia antes del `try`: un `rollback` expira los atributos de
    # la fila, y leerlo dentro del `except` para armar el mensaje de error
    # dispararía una consulta que en `AsyncSession` es `MissingGreenlet`.
    chain_name = chain.display_name
    fields = payload.model_dump(exclude={"chain_slug"})
    try:
        row = await CustomStoreRepository(session, user.id).create(
            {**fields, "chain_id": chain.id}
        )
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Ya cargaste una sucursal de {chain_name} llamada «{payload.name}».",
        ) from exc
    return _custom_out(row, chain)


@router.patch(
    "/custom-stores/{store_id}",
    response_model=CustomStoreOut,
    summary="Editar una sucursal cargada a mano",
    tags=["mapa"],
)
async def update_custom_store(
    session: DbSession,
    user: CurrentUser,
    store_id: int,
    payload: CustomStoreUpdate,
) -> CustomStoreOut:
    repo = CustomStoreRepository(session, user.id)
    row = await repo.by_id(store_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Sucursal inexistente")

    chain = await ChainRepository(session).by_id(row.chain_id)
    if chain is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Sucursal inexistente")

    try:
        await repo.update(row, payload.model_dump(exclude_unset=True))
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Ya cargaste una sucursal con ese nombre."
        ) from exc
    return _custom_out(row, chain)


@router.delete(
    "/custom-stores/{store_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Borrar una sucursal cargada a mano",
    tags=["mapa"],
)
async def delete_custom_store(
    session: DbSession, user: CurrentUser, store_id: int
) -> None:
    repo = CustomStoreRepository(session, user.id)
    row = await repo.by_id(store_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Sucursal inexistente")
    await repo.delete(row)
    await session.commit()
