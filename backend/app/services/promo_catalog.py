"""Carga del catálogo curado de promociones bancarias y de BIN por emisor.

Los datos viven en YAML versionado y no en base a propósito, por el mismo motivo
que `stores.py` tiene sus flags en código: un dato que se edita a mano necesita
diff y revisión. Una promo mal cargada no rompe nada visible —manda a comprar al
super equivocado con un número perfectamente creíble—, así que el único control
posible es que el cambio se vea en el historial.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.config import BACKEND_DIR
from app.models.payment import (
    Channel,
    PaymentInstrument,
    PaymentPromoRule,
    clean_bins,
)

logger = logging.getLogger(__name__)

DATA_DIR = BACKEND_DIR / "data"
PROMOS_PATH = DATA_DIR / "payment_promos.yaml"
BINS_PATH = DATA_DIR / "bins.yaml"

STALE_AFTER_DAYS = 60
"""A partir de acá una regla es sospechosa. No se descarta sola —eso escondería
el problema— pero se avisa, y el test `live` lo convierte en una falla."""


class IssuerBins(BaseModel):
    """Los BIN conocidos de un emisor."""

    model_config = ConfigDict(frozen=True)

    issuer_slug: str
    display_name: str
    bins: tuple[str, ...] = ()
    verified_on: date | None = None
    notes: str | None = None

    _normalize_bins = field_validator("bins", mode="before")(clean_bins)


@dataclass(frozen=True, slots=True)
class PromoCatalog:
    """Reglas y BIN cargados, ya validados."""

    rules: tuple[PaymentPromoRule, ...] = ()
    issuers: tuple[IssuerBins, ...] = ()

    def bins_for(self, issuer_slug: str) -> tuple[str, ...]:
        for issuer in self.issuers:
            if issuer.issuer_slug == issuer_slug:
                return issuer.bins
        return ()

    def issuer(self, issuer_slug: str) -> IssuerBins | None:
        return next(
            (i for i in self.issuers if i.issuer_slug == issuer_slug), None
        )

    def rules_for(
        self,
        instrument: PaymentInstrument,
        *,
        chain_slug: str,
        channel: Channel,
        on_date: date,
    ) -> list[PaymentPromoRule]:
        """Reglas que alcanzan a este medio de pago ese día, en esa cadena."""
        return [
            rule
            for rule in self.rules
            if rule.is_valid_on(on_date)
            and rule.matches(instrument, chain_slug=chain_slug, channel=channel)
        ]

    def stale(self, today: date | None = None) -> list[PaymentPromoRule]:
        """Reglas cuya verificación quedó vieja o cuya vigencia ya venció."""
        today = today or date.today()
        return [
            rule
            for rule in self.rules
            if (today - rule.verified_on).days > STALE_AFTER_DAYS
            or rule.valid_to < today
        ]


def _load_yaml(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        logger.warning("No existe %s: se sigue sin ese catálogo", path)
        return []
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError(f"{path}: se esperaba una lista de entradas")
    return raw


def load_catalog(
    promos_path: Path | None = None, bins_path: Path | None = None
) -> PromoCatalog:
    """Lee y valida los YAML.

    **Levanta excepción si un archivo está mal.** Arrancar ignorando la regla
    rota dejaría la app corriendo y contestando totales sin ese descuento, que es
    el modo de falla silencioso que hay que evitar; que no arranque es ruidoso y
    se arregla en el momento.
    """
    rules = [
        PaymentPromoRule.model_validate(entry)
        for entry in _load_yaml(promos_path or PROMOS_PATH)
    ]
    if duplicated := _duplicates(rule.id for rule in rules):
        raise ValueError(f"IDs de promo repetidos: {sorted(duplicated)}")

    issuers = [
        IssuerBins.model_validate(entry)
        for entry in _load_yaml(bins_path or BINS_PATH)
    ]
    if duplicated := _duplicates(issuer.issuer_slug for issuer in issuers):
        raise ValueError(f"Emisores repetidos en bins.yaml: {sorted(duplicated)}")

    catalog = PromoCatalog(rules=tuple(rules), issuers=tuple(issuers))

    if unverified := [r.id for r in catalog.rules if r.unverified]:
        logger.warning(
            "Promos sin verificar contra la fuente: %s. Sus ahorros son "
            "estimaciones basadas en datos que nadie confirmó.",
            ", ".join(unverified),
        )
    if stale := [r.id for r in catalog.stale()]:
        logger.warning(
            "Promos vencidas o sin verificar hace más de %d días: %s",
            STALE_AFTER_DAYS,
            ", ".join(stale),
        )
    return catalog


def _duplicates(values: Iterable[str]) -> set[str]:
    seen: set[str] = set()
    return {value for value in values if value in seen or seen.add(value)}  # type: ignore[func-returns-value]


@lru_cache(maxsize=1)
def get_catalog() -> PromoCatalog:
    """Catálogo cacheado para el proceso. `get_catalog.cache_clear()` lo recarga."""
    return load_catalog()


def known_issuers(catalog: PromoCatalog | None = None) -> Sequence[IssuerBins]:
    return (catalog or get_catalog()).issuers
