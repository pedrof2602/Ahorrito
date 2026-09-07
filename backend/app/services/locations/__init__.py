"""Dónde queda cada sucursal.

Ninguna de las cadenas publica esto junto con los precios, y cada una lo publica
distinto:

* **Carrefour y Día** exponen el endpoint de puntos de retiro de VTEX, que ya trae
  coordenadas. No hay nada que geocodificar.
* **Coto** publica una tabla HTML con la dirección en texto y el número de
  sucursal. Hay que geocodificar.
* **Disco** no publica nada: su `pickup-points` devuelve `total: 0`. No es un
  error ni algo que se pueda arreglar acá.

Por eso `Location` lleva `source`: una coordenada que publicó la cadena y una que
salió de geocodificar un texto no valen lo mismo, y esa diferencia tiene que
llegar hasta la pantalla en vez de perderse en el camino.
"""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass

SOURCE_API = "api"
"""La cadena publicó la coordenada."""

SOURCE_GEOCODED = "geocoded"
"""La coordenada salió de geocodificar una dirección en texto: es aproximada."""

SOURCE_MANUAL = "manual"
"""La cargó el usuario a mano. Es la más confiable de las tres —alguien que
conoce el lugar lo marcó— y por eso una sucursal manual le gana a la automática
de la misma cadena aunque quede un poco más lejos."""


@dataclass(frozen=True, slots=True)
class Location:
    """Una sucursal ubicada.

    `external_id` es la clave contra la que se vincula con el catálogo de
    precios, cuando existe: el `sellerId` en VTEX, el número de sucursal en Coto.
    """

    chain_slug: str
    external_id: str
    name: str
    latitude: float
    longitude: float
    source: str
    address: str | None = None
    city: str | None = None
    province: str | None = None
    postal_code: str | None = None
    """El CP donde está el local. No todas las fuentes lo publican."""


def strip_accents(text: str) -> str:
    return "".join(
        ch for ch in unicodedata.normalize("NFD", text) if not unicodedata.combining(ch)
    )


def normalize_name(text: str) -> str:
    """Forma comparable de un nombre de sucursal.

    Hace falta de verdad, no por prolijidad: los nombres que publica Carrefour
    vienen con espacios de más y sin criterio —`Market Maipú ` con espacio final,
    ` Market Mosconi ` con espacios de los dos lados, `Market  Jujuy I` con dos
    espacios en el medio—, así que comparar los strings crudos no matchea ni la
    sucursal consigo misma entre dos endpoints de la misma cadena.
    """
    lowered = strip_accents(str(text or "")).lower()
    # Todo lo que no sea letra o número pasa a ser separador: así `Hogar & Electro`
    # y `hogar y electro` no se diferencian por el símbolo.
    return re.sub(r"[^a-z0-9]+", " ", lowered).strip()


def normalize_query(text: str) -> str:
    """Clave de caché para una consulta de geocodificación.

    Se normaliza para que la misma dirección escrita con otro espaciado o en otra
    caja no gaste un request nuevo, que a un request por segundo se nota.
    """
    return re.sub(r"\s+", " ", str(text or "").strip()).lower()[:300]


EARTH_RADIUS_KM = 6371.0088


def haversine_km(
    lat1: float, lon1: float, lat2: float, lon2: float
) -> float:
    """Distancia en kilómetros entre dos puntos.

    Se calcula acá y no en SQL a propósito: SQLite no trae funciones
    trigonométricas por defecto, y meter la fórmula en la query ataría el
    proyecto a un motor. Son ~1.250 sucursales; en Python es instantáneo.
    """
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))
