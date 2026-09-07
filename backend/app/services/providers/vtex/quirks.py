"""Rarezas de serialización de VTEX.

Todo lo de acá existe porque una de las dos tiendas devuelve algo inesperado.
Cada función cita el caso que la justifica.

`normalize_ean` y `to_cents` se mudaron a `providers.common` cuando entró Coto:
no son rarezas de VTEX sino normalizaciones que necesita cualquier proveedor. Se
reexportan acá para no romper a quien ya las importaba de este módulo.
"""

from __future__ import annotations

import re
from typing import Any

from app.services.providers.common import normalize_ean, to_cents

__all__ = ["normalize_ean", "parse_resources", "to_cents", "unmangle"]

_BACKING_FIELD = re.compile(r"^<(?P<name>.+)>k__BackingField$")

# `resources: 0-1/1566` en ambas tiendas; la documentación de VTEX muestra
# `products 0-49/1234`. Aceptamos las dos formas.
_RESOURCES = re.compile(r"(?:\w+\s+)?(\d+)\s*-\s*(\d+)\s*/\s*(\d+)")


def unmangle(node: Any) -> Any:
    """Renombra las claves con mangling de C# a su nombre real.

    Carrefour serializa `Teasers` con los backing fields expuestos::

        {"<Name>k__BackingField": "Tarjeta Carrefour 15%"}

    Se aplica en profundidad porque el mangling también aparece en
    `Conditions`, `Effects` y `Parameters` anidados.
    """
    if isinstance(node, dict):
        result = {}
        for key, value in node.items():
            match = _BACKING_FIELD.match(key)
            result[match["name"] if match else key] = unmangle(value)
        return result
    if isinstance(node, list):
        return [unmangle(item) for item in node]
    return node


def parse_resources(header_value: str | None) -> tuple[int, int, int] | None:
    """Extrae (desde, hasta, total) del header `resources`.

    Devuelve None si el header falta o no matchea, para que el llamador decida
    (no siempre viene, y su ausencia no es un error).
    """
    if not header_value:
        return None
    match = _RESOURCES.search(header_value)
    if not match:
        return None
    return int(match[1]), int(match[2]), int(match[3])


