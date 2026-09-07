"""Depuración de los candidatos que devuelve una cadena, antes de comparar.

Dos filtros independientes y en este orden, porque el segundo solo tiene sentido
sobre un grupo que ya es del mismo producto:

1. `names.filter_by_name` — saca lo que no es lo que se pidió.
2. `outliers.flag_price_outliers` — sobre lo que quedó, marca los precios que se
   despegan de la mediana.

`select_candidate` los encadena y devuelve el elegido junto con el detalle de
cada paso, para poder auditar por qué ganó ese y no otro.

Todavía no está enganchado a `services/basket.py`: son funciones puras, se
prueban y se calibran solas.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from statistics import median

from app.models.catalog import ProductOffer
from app.services.matching.names import (
    NameFilterResult,
    ScoredOffer,
    filter_by_name,
    normalize,
    score_name,
)
from app.services.matching.outliers import (
    OutlierResult,
    PricedOffer,
    flag_price_outliers,
)

logger = logging.getLogger(__name__)

__all__ = [
    "CandidateSelection",
    "NameFilterResult",
    "OutlierResult",
    "PricedOffer",
    "ScoredOffer",
    "filter_by_name",
    "flag_price_outliers",
    "normalize",
    "score_name",
    "select_candidate",
]


@dataclass(slots=True)
class CandidateSelection:
    """Lo elegido y el rastro de cómo se llegó ahí."""

    chosen: ProductOffer | None
    names: NameFilterResult
    prices: OutlierResult

    @property
    def rejected_by_name(self) -> list[ScoredOffer]:
        return self.names.discarded

    @property
    def flagged_prices(self) -> list[PricedOffer]:
        return self.prices.outliers


def select_candidate(
    term: str,
    offers: list[ProductOffer],
    *,
    min_score: float | None = None,
    tolerance_pct: float | None = None,
    min_sample: int | None = None,
) -> CandidateSelection:
    """El más barato entre los que pasan el nombre y no quedan como outlier.

    La mediana de *todos* los candidatos —antes del filtro de nombre— se usa
    como referencia cuando el filtro deja uno solo: es lo único que permite
    avisar que ese precio no cierra sin descartarlo, que es la regla para el
    candidato único.
    """
    names = filter_by_name(term, offers, min_score=min_score)

    reference: int | None = None
    if len(names.offers) < 2 and len(offers) > 2:
        reference = round(median(o.offer.price_cents for o in offers))

    prices = flag_price_outliers(
        term,
        names.offers,
        tolerance_pct=tolerance_pct,
        min_sample=min_sample,
        reference_median_cents=reference,
    )
    return CandidateSelection(chosen=prices.cheapest, names=names, prices=prices)
