"""Puntos de retiro de VTEX: sucursales con coordenadas, sin geocodificar.

`GET /api/checkout/pub/pickup-points?geoCoordinates=lon;lat` devuelve, por cada
punto, nombre, dirección completa y coordenadas. Es el mismo endpoint que usa el
checkout para ofrecer retiro en tienda, así que es público y estable.

Medido contra las tres cadenas VTEX del proyecto:

* Carrefour: 390 puntos.
* Día: 744 puntos.
* Disco: **0**. No es un error nuestro ni algo que se pueda arreglar acá; Disco
  simplemente no publica sus sucursales, igual que no publica regiones ni acepta
  simulación de canasta.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.services.http import (
    AsyncRateLimiter,
    ProviderError,
    build_client,
    request_json,
)

from . import SOURCE_API, Location

logger = logging.getLogger(__name__)

PICKUP_PATH = "/api/checkout/pub/pickup-points"

PAGE_SIZE = 30
"""Lo que devuelve VTEX por página. No es configurable desde el query."""

MAX_PAGES = 40
"""Tope de seguridad. Con 744 puntos (Día, el más grande) alcanzan 25."""

SEED_POINTS: tuple[tuple[str, float, float], ...] = (
    ("CABA", -34.6037, -58.3816),
    ("La Plata", -34.9215, -57.9545),
    ("Rosario", -32.9442, -60.6505),
    ("Córdoba", -31.4201, -64.1888),
    ("Mendoza", -32.8895, -68.8458),
    ("Mar del Plata", -38.0055, -57.5426),
    ("Tucumán", -26.8083, -65.2176),
    ("Neuquén", -38.9516, -68.0591),
)
"""Puntos desde los que se barre.

**Hace falta más de uno, y no es por las dudas.** VTEX ordena los resultados por
distancia al punto que le pasás y corta en `total`; barriendo solo desde CABA,
`Hiper Córdoba Colón` y `Market Mar del Plata II` —dos sucursales que el
comparador sí conoce— no aparecen. Los resultados se unen por `id`, así que las
zonas donde las siembras se solapan no duplican nada.
"""


def parse_pickup_point(item: dict[str, Any], chain_slug: str) -> Location | None:
    """Traduce un punto de retiro. Devuelve None si no se puede ubicar.

    **Las coordenadas de VTEX vienen `[longitud, latitud]`**, al revés de lo que
    espera Leaflet y de como se escriben en castellano. Invertirlas manda Buenos
    Aires a China, y como el par sigue siendo un par de números válidos no falla
    nada: simplemente el mapa queda mal. Por eso se desempaqueta con nombre y hay
    un test que lo fija.
    """
    point = item.get("pickupPoint") or {}
    address = point.get("address") or {}
    coords = address.get("geoCoordinates")

    if not isinstance(coords, list) or len(coords) != 2:
        return None
    longitude, latitude = coords
    if not isinstance(longitude, int | float) or not isinstance(latitude, int | float):
        return None
    # (0, 0) es el Atlántico frente a África: VTEX lo usa como "sin cargar".
    if latitude == 0 and longitude == 0:
        return None

    external_id = str(point.get("id") or "").strip()
    if not external_id:
        return None

    street = str(address.get("street") or "").strip()
    number = str(address.get("number") or "").strip()
    street_line = " ".join(part for part in (street, number) if part) or None

    return Location(
        chain_slug=chain_slug,
        external_id=external_id,
        name=str(point.get("friendlyName") or external_id).strip(),
        latitude=float(latitude),
        longitude=float(longitude),
        source=SOURCE_API,
        address=street_line,
        city=str(address.get("city") or "").strip() or None,
        province=str(address.get("state") or "").strip() or None,
        postal_code=str(address.get("postalCode") or "").strip() or None,
    )


async def fetch_from_seed(
    client: httpx.AsyncClient,
    limiter: AsyncRateLimiter,
    chain_slug: str,
    latitude: float,
    longitude: float,
) -> list[Location]:
    """Todos los puntos alcanzables desde una coordenada, paginando."""
    found: list[Location] = []
    for page in range(1, MAX_PAGES + 1):
        body, _ = await request_json(
            client,
            "GET",
            PICKUP_PATH,
            limiter=limiter,
            params={
                "geoCoordinates": f"{longitude};{latitude}",
                "page": page,
            },
        )
        items = (body or {}).get("items") or []
        if not items:
            break

        for item in items:
            location = parse_pickup_point(item, chain_slug)
            if location is not None:
                found.append(location)

        paging = (body or {}).get("paging") or {}
        if page >= int(paging.get("pages") or 0):
            break
    return found


async def fetch_locations(
    chain_slug: str,
    base_url: str,
    *,
    user_agent: str,
    timeout_s: float = 20.0,
    min_interval_ms: int = 250,
    seeds: tuple[tuple[str, float, float], ...] = SEED_POINTS,
) -> list[Location]:
    """Sucursales ubicadas de una cadena VTEX, sin repetir.

    Una siembra que falla no tumba las demás: se avisa y se sigue. Media
    cobertura es mejor que ninguna, y el mapa ya sabe convivir con sucursales sin
    ubicar.
    """
    limiter = AsyncRateLimiter(max_concurrency=2, min_interval_ms=min_interval_ms)
    by_id: dict[str, Location] = {}

    client = build_client(
        base_url,
        user_agent=user_agent,
        timeout_s=timeout_s,
        max_concurrency=2,
    )
    async with client:
        for label, latitude, longitude in seeds:
            try:
                for location in await fetch_from_seed(
                    client, limiter, chain_slug, latitude, longitude
                ):
                    by_id.setdefault(location.external_id, location)
            except ProviderError as exc:
                logger.warning(
                    "%s: falló la siembra %s (%s). Se sigue con las demás.",
                    chain_slug,
                    label,
                    exc,
                )

    return sorted(by_id.values(), key=lambda loc: loc.external_id)
