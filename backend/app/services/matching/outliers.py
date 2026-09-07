"""Filtro 2: marca los precios que se despegan de la mediana del grupo.

Corre *después* del filtro de nombre, sobre candidatos que ya se supone que son
el mismo producto. Ahí un precio muy lejos del resto ya no es "una cadena más
barata": es un candidato mal matcheado que el nombre no alcanzó a filtrar —el
sachet en la búsqueda del litro, el pack de 6 en la del envase suelto— o un
precio que la cadena publica mal.

**Por qué la mediana y no el promedio.** El promedio se lo lleva puesto el mismo
outlier que se busca detectar: con tres precios de $2000 y uno de $200, el
promedio baja a $1550 y el barato deja de parecer raro. La mediana no se mueve.

**Por qué no descarta con muestras chicas.** Con dos candidatos la mediana es el
punto medio, así que ambos se desvían lo mismo en direcciones opuestas: o
ninguno es outlier o lo son los dos, y en ningún caso se aprendió nada sobre
cuál está mal. Por debajo de `PRICE_OUTLIER_MIN_SAMPLE` el filtro observa y
loguea, pero no descarta.

Nada se descarta en silencio: cada caso sale por `logger.warning` con producto,
precio y desvío, que es lo que hace falta para revisarlo después y para decidir
si la tolerancia quedó corta o larga.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from statistics import median

from app.core.config import settings
from app.models.catalog import ProductOffer

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PricedOffer:
    """Un candidato con su desvío respecto de la mediana del grupo."""

    offer: ProductOffer
    deviation_pct: float
    is_outlier: bool

    @property
    def name(self) -> str:
        return self.offer.product.name

    @property
    def price_cents(self) -> int:
        return self.offer.offer.price_cents


@dataclass(slots=True)
class OutlierResult:
    term: str
    tolerance_pct: float
    median_cents: int | None = None
    kept: list[PricedOffer] = field(default_factory=list)
    outliers: list[PricedOffer] = field(default_factory=list)
    enforced: bool = False
    """Si el descarte llegó a aplicarse.

    False cuando la muestra fue demasiado chica, cuando no había mediana usable
    o cuando descartar habría vaciado el grupo. En esos casos `outliers` puede
    tener elementos que igual siguen en `kept`: quedaron marcados para revisar,
    no excluidos.
    """

    @property
    def offers(self) -> list[ProductOffer]:
        return [p.offer for p in self.kept]

    @property
    def cheapest(self) -> ProductOffer | None:
        """El más barato de los que sobrevivieron: el resultado que usa el
        comparador."""
        if not self.kept:
            return None
        return min(self.kept, key=lambda p: p.price_cents).offer


def _pesos(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def _log_outlier(term: str, priced: PricedOffer, median_cents: int, *, descartado: bool) -> None:
    logger.warning(
        "Precio sospechoso en %s para '%s': %r a %s se desvía %+.1f%% de la "
        "mediana %s — %s",
        priced.offer.product.chain_slug,
        term,
        priced.name,
        _pesos(priced.price_cents),
        priced.deviation_pct,
        _pesos(median_cents),
        "descartado" if descartado else "se conserva, solo queda advertido",
    )


def flag_price_outliers(
    term: str,
    offers: list[ProductOffer],
    *,
    tolerance_pct: float | None = None,
    min_sample: int | None = None,
    reference_median_cents: int | None = None,
) -> OutlierResult:
    """Marca —y cuando la muestra alcanza, descarta— los precios desviados.

    `reference_median_cents` permite juzgar un grupo demasiado chico contra una
    mediana traída de afuera (por ejemplo la de todos los candidatos antes del
    filtro de nombre, o la de las otras cadenas). Sirve para el caso que más
    importa: cuando queda **un solo** candidato no hay con qué compararlo, así
    que nunca se lo descarta, pero con una referencia externa al menos se puede
    avisar que su precio no cierra.
    """
    tolerance = (
        settings.PRICE_OUTLIER_TOLERANCE_PCT if tolerance_pct is None else tolerance_pct
    )
    sample_floor = settings.PRICE_OUTLIER_MIN_SAMPLE if min_sample is None else min_sample
    result = OutlierResult(term=term, tolerance_pct=tolerance)

    if not offers:
        return result

    prices = [o.offer.price_cents for o in offers]
    center = reference_median_cents if len(offers) < sample_floor else None
    if center is None:
        center = round(median(prices))
    result.median_cents = center

    if center <= 0:
        # Sin mediana positiva no hay porcentaje que calcular. Pasa cuando la
        # cadena publica precio 0 en un producto sin stock.
        logger.warning(
            "No se pudo evaluar el precio de los %d candidatos para '%s': "
            "la mediana es %s",
            len(offers),
            term,
            _pesos(center),
        )
        result.kept = [PricedOffer(o, 0.0, False) for o in offers]
        return result

    evaluated = [
        PricedOffer(
            offer=offer,
            deviation_pct=(offer.offer.price_cents - center) * 100 / center,
            is_outlier=abs((offer.offer.price_cents - center) * 100 / center) > tolerance,
        )
        for offer in offers
    ]
    suspects = [p for p in evaluated if p.is_outlier]

    # --- muestra insuficiente: se avisa, no se descarta ---------------------
    if len(offers) < sample_floor:
        for priced in suspects:
            _log_outlier(term, priced, center, descartado=False)
        if len(offers) == 1 and reference_median_cents is None:
            logger.info(
                "Único candidato para '%s' en %s: su precio (%s) no se puede "
                "contrastar contra nada",
                term,
                offers[0].product.chain_slug,
                _pesos(prices[0]),
            )
        result.kept = evaluated
        result.outliers = suspects
        return result

    survivors = [p for p in evaluated if not p.is_outlier]

    # --- todos desviados: el grupo está partido, no hay a quién creerle -----
    if not survivors:
        logger.warning(
            "Los %d candidatos para '%s' en %s se desvían más de %.0f%% de su "
            "propia mediana (%s): se conservan todos y queda para revisar",
            len(offers),
            term,
            offers[0].product.chain_slug,
            tolerance,
            _pesos(center),
        )
        for priced in suspects:
            _log_outlier(term, priced, center, descartado=False)
        result.kept = evaluated
        result.outliers = suspects
        return result

    for priced in suspects:
        _log_outlier(term, priced, center, descartado=True)

    result.kept = survivors
    result.outliers = suspects
    result.enforced = True
    return result
