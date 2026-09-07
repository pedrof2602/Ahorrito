"""Geocodificación con Nominatim (OpenStreetMap).

Nominatim es gratis y no pide API key, pero su política de uso es explícita y hay
que respetarla o cortan el acceso:

* **Un request por segundo como máximo.** No es una sugerencia.
* **User-Agent que identifique la aplicación.** Un UA de navegador genérico es
  motivo de bloqueo.

De ahí dos decisiones de este módulo. El limitador va a 1.100 ms —con margen, no
justo en el límite— y **todo resultado se cachea en la base**, incluso los que no
resolvieron: una dirección que Nominatim no conoce, si no se cachea, se
reintenta en cada corrida y gasta el segundo de espera para volver a no obtener
nada.

Geocodificar es aproximado. Una coordenada de acá puede caer a media cuadra o en
la esquina de enfrente, y por eso las sucursales que pasan por este módulo quedan
marcadas con `location_source='geocoded'` en lugar de mezclarse con las que
publicó la propia cadena.
"""

from __future__ import annotations

import logging
from dataclasses import replace

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories import GeocodeCacheRepository
from app.services.http import (
    AsyncRateLimiter,
    ProviderError,
    build_client,
    request_json,
)

from . import Location

logger = logging.getLogger(__name__)

BASE_URL = "https://nominatim.openstreetmap.org"
SEARCH_PATH = "/search"

MIN_INTERVAL_MS = 1100
"""Política de Nominatim: 1 request/segundo. Los 100 ms de más son el margen."""

USER_AGENT = "ComprasApp/1.0 (comparador de precios de supermercados, uso personal)"
"""Nominatim exige un UA que identifique la app. El UA de navegador que usan los
proveedores de precios no sirve acá: con ese, bloquean."""


class Geocoder:
    """Resuelve direcciones a coordenadas, pasando primero por la caché."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        user_agent: str = USER_AGENT,
        timeout_s: float = 20.0,
        min_interval_ms: int = MIN_INTERVAL_MS,
    ) -> None:
        self._cache = GeocodeCacheRepository(session)
        self._session = session
        self._limiter = AsyncRateLimiter(
            max_concurrency=1, min_interval_ms=min_interval_ms
        )
        self._client = build_client(
            BASE_URL,
            user_agent=user_agent,
            timeout_s=timeout_s,
            max_concurrency=1,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> Geocoder:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def resolve(self, query: str) -> tuple[float, float] | None:
        """Coordenadas de una dirección, o None si no se pudo resolver.

        La caché contesta antes de salir a la red, y también recuerda los
        fracasos.
        """
        cached = await self._cache.get(query)
        if cached is not None:
            if cached.latitude is None or cached.longitude is None:
                return None
            return cached.latitude, cached.longitude

        try:
            body, _ = await request_json(
                self._client,
                "GET",
                SEARCH_PATH,
                limiter=self._limiter,
                params={
                    "q": query,
                    "format": "json",
                    "limit": 1,
                    "countrycodes": "ar",
                },
                # Un solo intento: si Nominatim no contesta, insistir es
                # exactamente lo que su política pide no hacer.
                max_retries=1,
            )
        except ProviderError as exc:
            # No se cachea: el fallo es de la red o del servicio, no de la
            # dirección, y cachearlo la marcaría como irresoluble para siempre.
            logger.warning("Nominatim falló para %r: %s", query, exc)
            return None

        if not isinstance(body, list) or not body:
            await self._cache.put(query, None, None)
            logger.info("Nominatim no conoce %r", query)
            return None

        first = body[0]
        try:
            latitude = float(first["lat"])
            longitude = float(first["lon"])
        except (KeyError, TypeError, ValueError):
            await self._cache.put(query, None, None)
            return None

        await self._cache.put(
            query, latitude, longitude, display_name=first.get("display_name")
        )
        return latitude, longitude

    async def locate(self, location: Location) -> Location | None:
        """Completa las coordenadas de una sucursal scrapeada."""
        query = address_query(location)
        if query is None:
            return None
        coords = await self.resolve(query)
        if coords is None:
            return None
        return replace(location, latitude=coords[0], longitude=coords[1])


def address_query(location: Location) -> str | None:
    """La consulta con la que se busca una sucursal.

    Se le agrega la localidad y el país porque sin eso Nominatim no desambigua:
    hay una calle Maipú en CABA y una ciudad Maipú en Mendoza, y sin contexto
    puede devolver cualquiera de las dos con la misma confianza.
    """
    if not location.address:
        return None
    parts = [location.address, location.city, location.province, "Argentina"]
    return ", ".join(part for part in parts if part)
