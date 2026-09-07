"""Sucursales de Coto, desde la tabla de `coto.com.ar/sucursales`.

Coto no tiene un endpoint de sucursales: publica una tabla HTML. Trae el número
de sucursal, el barrio, la dirección y los horarios — pero **no coordenadas**, así
que estas direcciones son las únicas del proyecto que hay que geocodificar.

Se parsea por el atributo `data-label` de cada celda (`Suc`, `Barrio`,
`Direccion`) y no por la posición de la columna: el `data-label` es lo que la
tabla usa para su versión mobile, así que está en el markup por un motivo propio
y no se mueve si agregan una columna en el medio.

Una advertencia medida, importante si alguna vez se quiere usar esto para
precios: **los números de sucursal de acá no cubren los del array de precios**.
La sucursal de referencia de Coto (`200`) y la que publica el precio por unidad
roto (`133`) no están en esta tabla. Alcanza para poner un marker en el mapa; no
alcanza para atarle un precio a una sucursal.
"""

from __future__ import annotations

import html
import logging
import re

from app.services.http import AsyncRateLimiter, build_client, classify

from . import SOURCE_GEOCODED, Location

logger = logging.getLogger(__name__)

SUCURSALES_URL = "https://www.coto.com.ar/sucursales/"

_ROW_RE = re.compile(r"<tr>(.*?)</tr>", re.S | re.I)
_CELL_RE = re.compile(r'data-label="([^"]+)"\s*>(.*?)</td>', re.S | re.I)
_TAG_RE = re.compile(r"<[^>]+>")


def _text(raw: str) -> str:
    """Texto plano de una celda.

    Se corta en el primer salto de línea porque la celda de dirección trae, a
    continuación del texto, un `<img>` con el ícono de autocobro; sin cortar,
    la dirección se lleva basura pegada que después arruina la geocodificación.
    """
    without_tags = _TAG_RE.sub("\n", raw)
    first_line = html.unescape(without_tags).split("\n")[0]
    return re.sub(r"\s+", " ", first_line).strip()


def parse_stores(markup: str) -> list[Location]:
    """Las sucursales de la tabla, sin coordenadas todavía.

    Salen con `source=geocoded` y coordenadas en cero: es el geocodificador el
    que las completa. Se devuelven igual —en vez de esconderlas hasta tener
    coordenada— para que el script pueda informar cuántas encontró y cuántas
    pudo ubicar, que son dos números distintos.
    """
    found: dict[str, Location] = {}

    for row in _ROW_RE.findall(markup):
        cells = {label: _text(value) for label, value in _CELL_RE.findall(row)}
        external_id = cells.get("Suc", "")
        address = cells.get("Direccion", "")
        if not external_id.isdigit() or not address:
            continue

        # "Agüero 616 - CAPITAL FEDERAL" -> calle y localidad
        street, _, city = address.partition(" - ")
        neighbourhood = cells.get("Barrio", "")

        found[external_id] = Location(
            chain_slug="coto-ar",
            external_id=external_id,
            name=f"Coto {neighbourhood.title()}" if neighbourhood else f"Coto {external_id}",
            latitude=0.0,
            longitude=0.0,
            source=SOURCE_GEOCODED,
            address=street.strip() or None,
            city=city.strip().title() or None,
        )

    return sorted(found.values(), key=lambda loc: int(loc.external_id))


async def fetch_stores(
    *, user_agent: str, timeout_s: float = 30.0, url: str = SUCURSALES_URL
) -> list[Location]:
    """Descarga y parsea la tabla.

    No usa `request_json` como el resto del proyecto porque esto es HTML: hay que
    pedir y clasificar a mano.
    """
    limiter = AsyncRateLimiter(max_concurrency=1, min_interval_ms=250)
    client = build_client(
        "https://www.coto.com.ar",
        user_agent=user_agent,
        timeout_s=timeout_s,
        max_concurrency=1,
        extra_headers={"Accept": "text/html,application/xhtml+xml"},
    )
    async with client:
        async with limiter.slot():
            response = await client.get(url)
    classify(response)
    return parse_stores(response.text)
