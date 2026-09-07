"""Geocodificación de direcciones argentinas con Georef (datos.gob.ar).

Es la API oficial del Estado argentino sobre el callejero del INDEC. Gratis, sin
API key y sin límite publicado de requests.

**Por qué esta y no Nominatim**, que ya está implementado al lado:

* *Llega.* `nominatim.openstreetmap.org` comparte las IPs de Fastly que varios
  ISP argentinos bloquean por rango — el mismo bloqueo que dejaba el mapa sin
  tiles. Desde una conexión así, Nominatim no contesta nunca. Georef está en
  otra infraestructura y responde en ~0,1 s.
* *Acierta.* Medido sobre direcciones reales: «Gallo 250, CABA» Georef lo pone
  en Comuna 3, que es donde está; Nominatim y Photon lo mandan a Sarmiento y a
  Banfield respectivamente, con toda confianza.

Lo que **no** hace, y por eso este módulo devuelve candidatos en vez de una
coordenada: desambiguar. «Av. Belgrano 950» sin más contexto existe en Tres
Arroyos, en Comuna 1 y en Saladillo, y Georef las devuelve a las tres con el
mismo aplomo. Elegir la primera es exactamente el error que pone un marker en la
cuadra equivocada. Acá se devuelve la lista y elige la persona, que es la única
que sabe cuál era.

Dos cosas medidas sobre los parámetros, porque no son obvias:

* `provincia` y `departamento` (el partido, o la comuna en CABA) filtran bien y
  no son sensibles a las tildes.
* `localidad` y `localidad_censal` **no** sirven con lo que uno escribiría:
  `localidad=Vicente Lopez` y `localidad=Capital Federal` devuelven cero
  resultados en direcciones que sí existen. Esperan nombres del padrón censal.
  Por eso acá el filtro fino va por `departamento` y, si deja la búsqueda sin
  resultados, se reintenta sin él en lugar de contestar «no existe».
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace

from app.db.repositories import GeocodeCacheRepository
from app.services.http import (
    AsyncRateLimiter,
    ProviderError,
    build_client,
    request_json,
)

from . import Location

logger = logging.getLogger(__name__)

BASE_URL = "https://apis.datos.gob.ar"
SEARCH_PATH = "/georef/api/direcciones"

USER_AGENT = "ComprasApp/1.0 (comparador de precios de supermercados, uso personal)"

MIN_INTERVAL_MS = 200
"""Georef no publica un límite. Se espacia igual: la API es un servicio público
y no hay ninguna razón para ser el que la satura."""

MAX_CANDIDATES = 8
"""Cuántas opciones se le ofrecen a quien está eligiendo. Más que esto deja de
ser una lista para elegir y pasa a ser una lista para leer."""


@dataclass(frozen=True, slots=True)
class Candidate:
    """Una dirección que Georef entendió, con dónde queda."""

    label: str
    """La `nomenclatura` de Georef, tal cual: «GALLO 250, Comuna 3, Ciudad
    Autónoma de Buenos Aires». Se muestra sin retocar porque es lo que deja ver
    que ubicó otra cosa — «AV BELGRANO 950, Tres Arroyos» salta a la vista."""

    latitude: float
    longitude: float
    city: str | None = None
    province: str | None = None


def _candidate(raw: object) -> Candidate | None:
    """Una dirección de la respuesta, o None si no vino ubicada.

    Georef puede devolver la calle sin `ubicacion` cuando conoce el nombre pero
    no la altura. Sirve para autocompletar, no para poner un marker.
    """
    if not isinstance(raw, dict):
        return None
    spot = raw.get("ubicacion")
    if not isinstance(spot, dict):
        return None
    try:
        latitude = float(spot["lat"])
        longitude = float(spot["lon"])
    except (KeyError, TypeError, ValueError):
        return None

    def name(key: str) -> str | None:
        value = raw.get(key)
        return value.get("nombre") if isinstance(value, dict) else None

    return Candidate(
        label=str(raw.get("nomenclatura") or "").strip() or "Sin nombre",
        latitude=latitude,
        longitude=longitude,
        city=name("departamento"),
        province=name("provincia"),
    )


def _dedupe(candidates: list[Candidate]) -> list[Candidate]:
    """Saca los repetidos por coordenada.

    Georef devuelve la misma esquina más de una vez cuando la calle figura en
    dos comunas: «GALLO 250» sale en Comuna 3 y en Comuna 5 con lat/lon
    idénticas. Son la misma opción escrita distinto, y ofrecer las dos obliga a
    elegir entre cosas que no se diferencian en nada.
    """
    seen: set[tuple[float, float]] = set()
    unique = []
    for candidate in candidates:
        key = (round(candidate.latitude, 6), round(candidate.longitude, 6))
        if key in seen:
            continue
        seen.add(key)
        unique.append(candidate)
    return unique


class GeorefGeocoder:
    """Busca direcciones argentinas y devuelve las opciones que encontró.

    Con `session` cachea en `geocode_cache`, igual que el cliente de Nominatim:
    lo usa la carga masiva de sucursales, donde volver a preguntar por las 122
    direcciones de Coto en cada corrida es gasto puro. La búsqueda interactiva
    la crea sin sesión —ahí el resultado es una lista para elegir, no una
    coordenada, y cachear la lista guardaría la pregunta en vez de la respuesta.
    """

    def __init__(self, *, session=None, timeout_s: float = 15.0) -> None:
        self._cache = GeocodeCacheRepository(session) if session is not None else None
        self._limiter = AsyncRateLimiter(
            max_concurrency=1, min_interval_ms=MIN_INTERVAL_MS
        )
        self._client = build_client(
            BASE_URL,
            user_agent=USER_AGENT,
            timeout_s=timeout_s,
            max_concurrency=1,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> GeorefGeocoder:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def _query(self, params: dict[str, object]) -> list[Candidate]:
        try:
            body, _ = await request_json(
                self._client,
                "GET",
                SEARCH_PATH,
                limiter=self._limiter,
                params=params,
                max_retries=2,
            )
        except ProviderError as exc:
            logger.warning("Georef falló para %r: %s", params.get("direccion"), exc)
            raise

        raw = body.get("direcciones") if isinstance(body, dict) else None
        if not isinstance(raw, list):
            return []
        return _dedupe([c for c in map(_candidate, raw) if c is not None])

    async def search(
        self,
        address: str,
        *,
        city: str | None = None,
        province: str | None = None,
        limit: int = MAX_CANDIDATES,
        strict: bool = False,
    ) -> list[Candidate]:
        """Las direcciones que coinciden, de la más probable a la menos.

        `city` viaja como `departamento` —el partido o la comuna— porque es lo
        que la gente escribe y lo que Georef sabe filtrar. Si con ese filtro no
        queda nada se vuelve a preguntar sin él: que el partido esté mal
        escrito, o que la calle figure en el de al lado, no es razón para
        contestar que la dirección no existe. Lo que se pierde es precisión en
        el orden, y eso lo resuelve quien elige mirando la lista.

        `strict` apaga ese reintento, y lo usa quien no tiene a nadie a quien
        preguntarle. Cuando la ciudad se sabe con certeza —viene escrita en el
        dato de origen— un resultado de otra ciudad no es una aproximación, es
        un error: «Urquiza 1644 (Rosario)» sin el filtro se resuelve a doscientos
        kilómetros de Rosario. Ahí conviene no devolver nada.
        """
        address = address.strip()
        if not address:
            return []

        base: dict[str, object] = {"direccion": address, "max": limit}
        if province:
            base["provincia"] = province.strip()

        if city and city.strip():
            narrow = await self._query({**base, "departamento": city.strip()})
            if narrow or strict:
                return narrow

        return await self._query(base)

    async def resolve(
        self,
        query: str,
        *,
        city: str | None = None,
        province: str | None = None,
        strict: bool = False,
    ) -> tuple[float, float] | None:
        """La primera coincidencia, para los llamadores que no pueden preguntar.

        Misma firma que `Geocoder.resolve` de Nominatim, a propósito: el script
        de carga masiva de sucursales no tiene a quién consultarle cuál de tres
        Belgrano 950 era. Ahí quedarse con la primera es lo único que se puede
        hacer, y por eso lo que sale de acá se marca `geocoded` y la UI lo
        presenta como aproximado.
        """
        # La clave lleva el rol de cada parte y no solo su texto. Sin eso,
        # buscar «Rivadavia 3396» con Santa Fe *como partido* y con Santa Fe
        # *como provincia* producen la misma clave, y la segunda —que es la
        # buena— se contesta con el resultado cacheado de la primera.
        key = f"georef|{query}|dep={city or ''}|prov={province or ''}"
        if self._cache is not None:
            cached = await self._cache.get(key)
            if cached is not None:
                if cached.latitude is None or cached.longitude is None:
                    return None
                return cached.latitude, cached.longitude

        try:
            candidates = await self.search(
                query, city=city, province=province, limit=1, strict=strict
            )
        except ProviderError:
            # No se cachea: el fallo es de la red, no de la dirección, y
            # guardarlo la marcaría como irresoluble para siempre.
            return None

        if not candidates:
            if self._cache is not None:
                # El fracaso sí se cachea: una dirección que el callejero no
                # conoce no la va a conocer en la corrida siguiente, y
                # reintentarla gasta un request para volver a no obtener nada.
                await self._cache.put(key, None, None)
            return None

        best = candidates[0]
        if self._cache is not None:
            await self._cache.put(
                key, best.latitude, best.longitude, display_name=best.label
            )
        return best.latitude, best.longitude

    async def locate(self, location: Location) -> Location | None:
        """Completa las coordenadas de una sucursal scrapeada.

        La localidad viaja como `departamento` —el partido o la comuna— que es
        el filtro que Georef sabe usar. Sin nada que acote, «Av. San Martín
        2095» de una sucursal porteña sale resuelto en Corrientes: medido.

        Va en modo estricto porque acá no hay nadie a quien preguntarle: una
        carga masiva que afloja el filtro no obtiene una respuesta peor, obtiene
        una respuesta equivocada que se ve igual de bien que las buenas.
        """
        if not location.address:
            return None
        coords = await self.resolve(
            location.address,
            city=location.city,
            province=location.province,
            strict=True,
        )
        if coords is None:
            return None
        return replace(location, latitude=coords[0], longitude=coords[1])
