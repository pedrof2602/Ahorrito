"""Cliente genérico para inquilinos VTEX (Carrefour, Disco, ...)."""

from .client import VTEXClient
from .config import RegionStrategy, VTEXStoreConfig
from .provider import VTEXProvider
from .stores import CARREFOUR_AR, DIA_AR, DISCO_AR, VTEX_STORES

__all__ = [
    "CARREFOUR_AR",
    "DIA_AR",
    "DISCO_AR",
    "VTEX_STORES",
    "RegionStrategy",
    "VTEXClient",
    "VTEXProvider",
    "VTEXStoreConfig",
]
