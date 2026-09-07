"""Qué te llevás realmente con una promo, en castellano.

Las cadenas publican la promo como un título pensado para el cartel de la
góndola —`"2x1"`, `"PROMO-2do al 70% Max 8 unidades Combinable LUCCHETTI"`,
`"2x$2500"`—, no como datos. Este módulo lo traduce a mecánica explícita y a una
frase que se pueda leer sin saber la jerga.

**Por qué importa más de lo que parece: el título miente por omisión.** "2do al
70%" suena a 70% de descuento y es 35% sobre el par; "3x2" suena a mucho y es
33%. Alguien comparando cadenas con esos números elige mal. Por eso cada
mecánica declara `effective_percent`, que es el ahorro sobre el combo completo y
la única cifra comparable entre promos de formas distintas.

────────────────────────────────────────────────────────────────────────────
LA SEMÁNTICA ESTÁ MEDIDA, NO SUPUESTA.

Cada forma se verificó simulando la compra contra el checkout real y comparando
el total contra el precio de lista (2026-09-01):

    2x1          sku 297590 DIA        x2  $11.350 -> $5.675    50,0%
    3x2          sku 269579 DIA        x3   $3.240 -> $2.160    33,3%
    6x4          sku 250909 DIA        x6  $15.600 -> $10.400   33,3%
    2do al 50%   sku 8922   Carrefour  x2   $2.880 -> $2.160    25,0%
    2do al 70%   sku 84975  DIA        x2  $11.800 -> $7.670    35,0%
    2do al 80%   sku 7523   Carrefour  x2   $4.398 -> $2.638,80 40,0%
    2x$2500      sku 205793 DIA        x2   $3.400 -> $2.500    26,5%

El caso "2do al X%" es el que justifica haber medido: 25 = 50/2, 35 = 70/2 y
40 = 80/2 confirman que el X es **descuento sobre la segunda unidad**, y no "la
segunda te sale el X% del precio". Las dos lecturas son gramaticalmente válidas
y dan resultados muy distintos.
────────────────────────────────────────────────────────────────────────────

El texto que devuelve `describe()` habla siempre de lo que la promo *ofrece*,
nunca de lo que vas a pagar. Medido: el checkout a veces aplica una promo mejor
que la anunciada (sku 3796 de Carrefour anuncia "2do al 50% Mi Crf" y liquida un
"2do al 70%") y a veces una peor o ninguna (sku 203735 anuncia "2do al 80%" y no
descuenta nada sobre el par). El teaser es una oferta, no una promesa.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.models.catalog import PromotionKind

__all__ = ["PromoMechanics", "membership_required", "parse_promo_text"]

# Marcas de pertenencia observadas en los títulos de promo del catálogo real
# (barrido del 2026-09-01 sobre 18 términos y las cuatro cadenas). Cada entrada
# es un marcador medido, no una marca que la cadena "debería" usar.
#
# Importa para el comparador: una promo que exige la tarjeta de la cadena no
# está disponible para cualquiera, y contarla como si lo estuviera le hace ganar
# una comparación que no gana.
_MEMBERSHIP_MARKERS: tuple[tuple[str, str], ...] = (
    # Carrefour. "Mi Crf" es como el catálogo abrevia Mi Carrefour: aparece en
    # "PROMO-2do al 50% Mi Crf Max 48 unidades Combinable FANTA".
    ("cuenta digital", "Cuenta Digital Carrefour"),
    ("mi crf", "Mi Carrefour"),
    ("mi carrefour", "Mi Carrefour"),
    ("tarjeta carrefour", "Tarjeta Carrefour"),
    # Coto: el mapper arma "Precio con tarjeta: $X" con los datos de
    # `discounts_payment_methods`, que es la TCI.
    ("precio con tarjeta", "Tarjeta de crédito Coto (TCI)"),
    ("tarjeta coto", "Tarjeta de crédito Coto (TCI)"),
    ("comunidad coto", "Comunidad Coto"),
    # DIA y Disco: sin marcadores observados en el barrido. Si aparecen, van acá
    # con la misma regla — se agrega lo que se ve, no lo que se supone.
    ("club dia", "Club DIA"),
    ("cencosud", "Tarjeta Cencosud"),
)


def membership_required(name: str) -> tuple[str, ...]:
    """Tarjetas o comunidades que habilitan la promo, según el título.

    Devuelve **todas** las que nombra, no la primera: "40% Off Tarjeta Carrefour
    o Cuenta digital" sirve con cualquiera de las dos, y quedarse con una sola
    le diría al usuario que no le alcanza cuando en realidad sí.

    Solo reconoce lo que se vio en el catálogo. La tupla vacía significa "el
    título no lo aclara", que no es lo mismo que "no hace falta nada".
    """
    lowered = (name or "").lower()
    found: list[str] = []
    for marker, display in _MEMBERSHIP_MARKERS:
        if marker in lowered and display not in found:
            found.append(display)
    return tuple(found)

# "2x1", "3x2", "6x4". Se exige que no venga precedido de "$" para no confundir
# con "2x$2500", que es otra promo distinta.
_N_FOR_M = re.compile(r"(?<![\w$])(\d{1,2})\s*[xX]\s*(\d{1,2})(?![\w%$])")

# "2x$2500", "3 x $ 1.100": N unidades a un precio total fijo.
_N_FOR_PRICE = re.compile(r"(?<![\w])(\d{1,2})\s*[xX]\s*\$\s*([\d][\d.,]*)")

# "2do al 70%", "3ra al 50%", "2da al 80 %".
_NTH_AT_PERCENT = re.compile(
    r"\b(\d{1,2})\s*(?:do|da|ro|ra|to|ta|er|°|º)?\s*al\s*(\d{1,3})\s*%", re.IGNORECASE
)

# "70% 2da", "50% 2do": la forma de Coto, con el porcentaje adelante.
_PERCENT_NTH = re.compile(
    r"(\d{1,3})\s*%\s*(?:de\s*dto\.?\s*)?(?:en\s*(?:la|el)\s*)?(\d{1,2})\s*(?:da|do|ra|ro|ta|to|°|º)\b",
    re.IGNORECASE,
)

_ORDINAL_ES = {
    2: "2da", 3: "3ra", 4: "4ta", 5: "5ta", 6: "6ta",
    7: "7ma", 8: "8va", 9: "9na", 10: "10ma",
}


def _ordinal(n: int) -> str:
    return _ORDINAL_ES.get(n, f"{n}ª")


def _money(cents: int) -> str:
    """$2.500 / $1.234,50 — separador de miles con punto, como se escribe acá."""
    entero, resto = divmod(abs(cents), 100)
    texto = f"{entero:,}".replace(",", ".")
    if resto:
        texto = f"{texto},{resto:02d}"
    return f"${texto}"


def _pct(value: float) -> str:
    """Sin decimal cuando es redondo: 33,3% pero 50%."""
    return f"{value:.1f}".replace(".0", "").replace(".", ",") + "%"


def _parse_money_ar(raw: str) -> int | None:
    """"2.500" -> 250000. El punto es separador de miles en los carteles."""
    cleaned = raw.replace(".", "").replace(",", ".")
    try:
        return round(float(cleaned) * 100)
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class PromoMechanics:
    """La mecánica de una promo, ya desarmada.

    `effective_percent` es el ahorro sobre el combo entero y es el único campo
    comparable entre formas: un "2do al 70%" (35%) rinde menos que un "3x2"
    (33%)... y ahí se ve que no, que rinde más, que es exactamente el tipo de
    cuenta que nadie hace de memoria en la góndola.
    """

    kind: PromotionKind
    bundle_quantity: int | None = None
    """Unidades que hay que llevar para que la promo se dispare."""

    paid_quantity: int | None = None
    """Unidades que se pagan. Solo en las NxM."""

    nth_unit_percent_off: float | None = None
    """Descuento sobre la unidad N. En "2do al 70%" es 70, no 35."""

    bundle_price_cents: int | None = None
    """Precio total fijo del combo. Solo en las Nx$."""

    effective_percent: float | None = None
    """Ahorro sobre el combo completo. La cifra que sirve para comparar."""

    def describe(self) -> str:
        """La promo explicada, sin jerga y sin prometer lo que no se sabe."""
        n = self.bundle_quantity

        if self.paid_quantity is not None and n is not None:
            gratis = n - self.paid_quantity
            if gratis == 1:
                cuerpo = (
                    f"Llevando {n} pagás {self.paid_quantity}: "
                    f"la {_ordinal(n)} unidad te la llevás gratis"
                )
            else:
                cuerpo = (
                    f"Llevando {n} pagás {self.paid_quantity}: "
                    f"{gratis} unidades gratis"
                )
        elif self.nth_unit_percent_off is not None and n is not None:
            cuerpo = (
                f"Llevando {n}, la {_ordinal(n)} unidad tiene "
                f"{_pct(self.nth_unit_percent_off)} de descuento"
            )
        elif self.bundle_price_cents is not None and n is not None:
            cuerpo = (
                f"Llevando {n} pagás {_money(self.bundle_price_cents)} "
                f"por las {n} juntas"
            )
        else:
            return ""

        if self.effective_percent is not None:
            cuerpo += f" ({_pct(self.effective_percent)} de ahorro llevando {n})"
        return cuerpo + "."


def _bundle_mechanics(name: str) -> PromoMechanics | None:
    """Las formas que no dependen del precio: NxM y "Ndo al X%"."""
    if match := _N_FOR_M.search(name):
        total, paid = int(match[1]), int(match[2])
        # "1x1" o "2x3" no son promos; son ruido que matchea la forma.
        if total <= paid or paid < 1:
            return None
        return PromoMechanics(
            kind=PromotionKind.BUY_X_GET_Y,
            bundle_quantity=total,
            paid_quantity=paid,
            effective_percent=round((total - paid) * 100 / total, 1),
        )

    match = _NTH_AT_PERCENT.search(name) or _PERCENT_NTH.search(name)
    if match:
        # Los dos patrones traen (unidad, porcentaje) en orden distinto.
        if match.re is _NTH_AT_PERCENT:
            nth, percent = int(match[1]), float(match[2])
        else:
            percent, nth = float(match[1]), int(match[2])
        if nth < 2 or percent <= 0 or percent > 100:
            return None
        return PromoMechanics(
            kind=PromotionKind.BUY_X_GET_Y,
            bundle_quantity=nth,
            nth_unit_percent_off=percent,
            # El descuento cae sobre una unidad de las N: sobre el combo rinde
            # percent/N. Es la corrección que evita publicar un 70% que no es.
            effective_percent=round(percent / nth, 1),
        )
    return None


def parse_promo_text(
    name: str, *, unit_price_cents: int | None = None
) -> PromoMechanics | None:
    """Desarma el título de una promo. None si no reconoce la forma.

    `unit_price_cents` solo hace falta para las `Nx$`: sin saber cuánto vale una
    unidad no hay forma de decir cuánto se ahorra pagando $2.500 por dos, y en
    ese caso la mecánica igual se devuelve, pero sin `effective_percent`.
    """
    if not name:
        return None

    # El precio fijo va primero: "2x$2500" también matchea la forma NxM si no se
    # lo saca antes del camino.
    if match := _N_FOR_PRICE.search(name):
        quantity = int(match[1])
        bundle_cents = _parse_money_ar(match[2])
        if quantity < 2 or bundle_cents is None or bundle_cents <= 0:
            return None
        effective: float | None = None
        if unit_price_cents and unit_price_cents > 0:
            bruto = unit_price_cents * quantity
            if 0 < bundle_cents < bruto:
                effective = round((bruto - bundle_cents) * 100 / bruto, 1)
        return PromoMechanics(
            kind=PromotionKind.BUY_X_GET_Y,
            bundle_quantity=quantity,
            bundle_price_cents=bundle_cents,
            effective_percent=effective,
        )

    return _bundle_mechanics(name)
