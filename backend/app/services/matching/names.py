"""Filtro 1: descarta los candidatos cuyo nombre no es lo que se pidió.

La búsqueda de una cadena devuelve lo que su motor considera relevante, y eso
incluye seguido productos que no son el pedido: para "leche" aparecen la
chocolatada, el dulce de leche y las galletitas sabor leche. Mientras el
comparador ancle por EAN el ruido no molesta, pero cuando cae al matcheo por
nombre —que es cuando ninguna cadena comparte EAN— ese ruido entra directo a la
comparación de precios y la ensucia.

**Qué mide el score.** La fracción del *término pedido* que aparece en el nombre
del candidato, ponderada por el largo de cada palabra. No es una similitud
simétrica, y es a propósito: los nombres de góndola traen ruido que el término
nunca va a tener ("Gaseosa … Sabor Original … x 30 M"), así que una métrica
simétrica castiga a los candidatos correctos por ser verbosos. Medido sobre
pares reales, `token_sort_ratio` hunde a los buenos a 28-89 y `token_set_ratio`
satura en 100 para cualquier cosa que contenga el término; la cobertura
ponderada deja los aciertos en 99-100 y los errores en 50-80.

**Límite que hay que conocer antes de mover el umbral.** Con un término de una
sola palabra el filtro es ciego: "Leche Entera" y "Leche Chocolatada" contienen
ambos el término entero y los dos puntúan 100. Distinguirlos es semántico, no
léxico, y ningún umbral lo resuelve. Contra ese caso siguen valiendo las
defensas que ya tiene `basket.py`: el anclaje por EAN y el orden de relevancia
de la cadena.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field

from rapidfuzz import fuzz

from app.core.config import settings
from app.models.catalog import Product, ProductOffer

logger = logging.getLogger(__name__)

_DIGIT_THEN_LETTER = re.compile(r"(\d)([a-z])")
_LETTER_THEN_DIGIT = re.compile(r"([a-z])(\d)")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def normalize(text: str) -> list[str]:
    """Pasa un nombre a los tokens con los que se lo compara.

    Saca acentos y puntuación, y separa número de unidad para que "1l" y "1 Lt"
    tengan la misma forma: sin eso, el token del envase —que suele ser lo único
    que distingue dos presentaciones del mismo producto— no matchea nunca.
    """
    lowered = unicodedata.normalize("NFKD", text.lower())
    stripped = "".join(c for c in lowered if not unicodedata.combining(c))
    spaced = _LETTER_THEN_DIGIT.sub(r"\1 \2", _DIGIT_THEN_LETTER.sub(r"\1 \2", stripped))
    return _NON_ALNUM.sub(" ", spaced).split()


def _candidate_text(product: Product) -> str:
    """El texto del candidato incluye la marca.

    Varias cadenas la dejan fuera del nombre ("Yerba Mate Con Palo 1 Kg" con
    `brand="Playadito"`), y sin ella un término que nombra la marca —lo normal
    en una lista de compras— puntuaría bajo contra el producto correcto.
    """
    return f"{product.name} {product.brand}" if product.brand else product.name


def score_name(term: str, product: Product) -> float:
    """Cuánto del término pedido aparece en el candidato, de 0 a 100.

    Cada palabra del término pesa lo que mide, así que "de" o "la" casi no
    mueven el resultado y "playadito" sí. El aporte de cada una es su mejor
    coincidencia contra alguna palabra del candidato: se busca presencia, no
    posición, porque las cadenas ordenan los nombres como quieren ("Fideos
    Guiseros Tirabuzon Matarazzo" contra "fideos matarazzo tirabuzon").
    """
    wanted = normalize(term)
    found = normalize(_candidate_text(product))
    if not wanted or not found:
        return 0.0

    total_weight = 0.0
    matched = 0.0
    for token in wanted:
        weight = float(len(token))
        total_weight += weight
        matched += weight * max(fuzz.ratio(token, other) for other in found)
    return matched / total_weight


@dataclass(frozen=True, slots=True)
class ScoredOffer:
    """Un candidato con el puntaje que sacó. El puntaje viaja para poder
    ajustar el umbral mirando datos y no adivinando."""

    offer: ProductOffer
    score: float

    @property
    def name(self) -> str:
        return self.offer.product.name


@dataclass(slots=True)
class NameFilterResult:
    term: str
    min_score: float
    kept: list[ScoredOffer] = field(default_factory=list)
    discarded: list[ScoredOffer] = field(default_factory=list)

    @property
    def offers(self) -> list[ProductOffer]:
        """Los candidatos que pasaron, en el orden de relevancia original."""
        return [s.offer for s in self.kept]

    @property
    def emptied(self) -> bool:
        """Había candidatos y el filtro no dejó ninguno.

        Es el síntoma de un umbral demasiado alto para ese término, y por eso se
        informa aparte en vez de confundirse con "la cadena no tiene el producto".
        """
        return not self.kept and bool(self.discarded)

    @property
    def best_discarded(self) -> ScoredOffer | None:
        """El descartado que estuvo más cerca: el número con el que se calibra
        el umbral cuando el filtro vació la lista."""
        return max(self.discarded, key=lambda s: s.score, default=None)


def filter_by_name(
    term: str,
    offers: list[ProductOffer],
    *,
    min_score: float | None = None,
) -> NameFilterResult:
    """Deja solo los candidatos cuyo nombre se parece a `term`.

    Se preserva el orden de entrada —el de relevancia de la cadena— porque el
    comparador lo usa para desempatar; ordenar por puntaje acá sería reemplazar
    el criterio de la cadena por uno propio sin haberlo medido.

    Si ningún candidato llega al umbral devuelve la lista vacía en lugar de
    quedarse con el menos malo: un mal match no se vuelve bueno por ser el mejor
    disponible, y la línea sin resolver es un resultado honesto —el comparador
    ya sabe informarla como faltante— mientras que un producto equivocado con
    precio es justo el número convincente y equivocado que hay que no dar.
    """
    threshold = settings.NAME_MATCH_MIN_SCORE if min_score is None else min_score
    result = NameFilterResult(term=term, min_score=threshold)

    for offer in offers:
        scored = ScoredOffer(offer=offer, score=score_name(term, offer.product))
        if scored.score >= threshold:
            result.kept.append(scored)
        else:
            result.discarded.append(scored)

    chain = offers[0].product.chain_slug if offers else "-"
    for scored in result.discarded:
        logger.debug(
            "Descartado por nombre en %s para '%s': %r (%.0f < %.0f)",
            chain,
            term,
            scored.name,
            scored.score,
            threshold,
        )
    if result.emptied:
        best = result.best_discarded
        logger.warning(
            "El filtro de nombre descartó los %d candidatos de %s para '%s'; "
            "el mejor fue %r con %.0f (umbral %.0f)",
            len(result.discarded),
            chain,
            term,
            best.name if best else "-",
            best.score if best else 0.0,
            threshold,
        )
    return result
