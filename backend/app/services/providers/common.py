"""Normalizaciones compartidas por todos los proveedores.

Acá vive lo que no es de ninguna cadena en particular: un GTIN es un GTIN en
VTEX y en Constructor.io, y pasar pesos a centavos tampoco depende del
proveedor. Las rarezas de serialización de cada cadena van en su propio módulo
(`vtex/quirks.py`), no acá.
"""

from __future__ import annotations

import re

# Códigos internos de peso variable: los usa la balanza, no identifican producto.
_INTERNAL_EAN_PREFIXES = ("2",)


def _gtin_check_digit(digits: str) -> int:
    """Dígito verificador GTIN: pesos 3 y 1 alternados de derecha a izquierda."""
    total = 0
    for index, char in enumerate(reversed(digits)):
        total += int(char) * (3 if index % 2 == 0 else 1)
    return (10 - total % 10) % 10


def normalize_ean(raw: str | int | None) -> str | None:
    """Normaliza un EAN a GTIN-13 válido, o None si no sirve para matchear.

    Un EAN mal normalizado es peor que ninguno: haría que dos productos
    distintos se fusionen y le mostraríamos al usuario el precio equivocado.
    Ante la duda, devolvemos None y que el matcheo lo resuelva por nombre.

    Acepta `int` porque Coto serializa `product_main_ean` como número
    (`7790742358608`, sin comillas) en las 250 respuestas medidas; VTEX lo manda
    como string. Normalizar el tipo acá evita que cada mapper lo recuerde.
    """
    if raw is None:
        return None
    digits = re.sub(r"\D", "", str(raw))
    if not digits or len(digits) > 14:
        return None
    if set(digits) == {"0"}:
        return None

    if len(digits) == 14 and digits[0] == "0":
        digits = digits[1:]
    if len(digits) in (8, 12):
        digits = digits.zfill(13)
    if len(digits) != 13:
        return None

    if _gtin_check_digit(digits[:12]) != int(digits[12]):
        return None
    # Códigos internos de balanza: el peso va dentro del código, así que el
    # mismo producto tiene un EAN distinto por paquete.
    if digits.startswith(_INTERNAL_EAN_PREFIXES):
        return None
    return digits


def to_cents(value: float | int | str | None) -> int | None:
    """Pasa un precio en pesos a centavos.

    `round` sobre el string evita el clásico 10000.9 -> 1000089.
    """
    if value is None:
        return None
    try:
        return round(float(value) * 100)
    except (TypeError, ValueError):
        return None
