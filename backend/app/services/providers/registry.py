"""Registro de proveedores de precios.

Único punto donde se instancian los clientes. Vive en el lifespan de la app para
que los pools de conexión se reutilicen entre requests en lugar de rearmarse.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable, Sequence
from dataclasses import replace

from app.core.config import settings
from app.models.catalog import Chain

from .base import PriceProvider
from .cache import CachedProvider
from .coto.client import CotoClient
from .coto.config import COTO_AR
from .coto.provider import CotoProvider
from .vtex.client import VTEXClient
from .vtex.provider import VTEXProvider
from .vtex.stores import VTEX_STORES

logger = logging.getLogger(__name__)


class ProviderRegistry:
    def __init__(self, providers: Iterable[PriceProvider]) -> None:
        self._providers: dict[str, PriceProvider] = {
            provider.chain.slug: provider for provider in providers
        }

    @classmethod
    def build(
        cls,
        enabled_slugs: Sequence[str] | None = None,
        *,
        cache: bool | None = None,
    ) -> ProviderRegistry:
        enabled = set(enabled_slugs or settings.ENABLED_CHAINS)
        providers: list[PriceProvider] = []
        for config in VTEX_STORES:
            if enabled and config.slug not in enabled:
                continue
            # Mismo `replace` que Coto, por el mismo motivo. Sin esto,
            # `HTTP_MIN_INTERVAL_MS` era un no-op para Carrefour, Disco y Día:
            # el `.env` decía a qué ritmo consultábamos y tres de las cuatro
            # cadenas seguían con el default del dataclass. Bajar el ritmo es
            # justo la perilla que uno quiere que funcione.
            vtex_config = replace(
                config,
                timeout_s=settings.HTTP_TIMEOUT_S,
                min_interval_ms=settings.HTTP_MIN_INTERVAL_MS,
            )
            client = VTEXClient(
                vtex_config,
                user_agent=settings.HTTP_USER_AGENT,
                max_retries=settings.HTTP_MAX_RETRIES,
            )
            providers.append(VTEXProvider(vtex_config, client))

        # Coto no es VTEX: habla la API de Constructor.io y se arma aparte.
        if not enabled or COTO_AR.slug in enabled:
            # Los límites HTTP comunes mandan sobre los defaults del proveedor:
            # son la política de "buen ciudadano" de la app, no de la cadena.
            coto_config = replace(
                COTO_AR,
                timeout_s=settings.HTTP_TIMEOUT_S,
                min_interval_ms=settings.HTTP_MIN_INTERVAL_MS,
            )
            providers.append(
                CotoProvider(
                    coto_config,
                    CotoClient(
                        coto_config,
                        user_agent=settings.HTTP_USER_AGENT,
                        max_retries=settings.HTTP_MAX_RETRIES,
                    ),
                )
            )

        logger.info(
            "Proveedores activos: %s",
            ", ".join(p.chain.slug for p in providers) or "(ninguno)",
        )

        # La caché se envuelve acá y no dentro de cada proveedor: es política de
        # la app —cada cuánto estamos dispuestos a creerle a un precio guardado—
        # y no algo que Carrefour o Coto tengan que saber. Envolviendo en el
        # registry, todo lo que consulte precios la hereda.
        if cache if cache is not None else settings.PRICE_CACHE_ENABLED:
            providers = [CachedProvider(p) for p in providers]
            logger.info(
                "Caché de precios activa: TTL %ds, respaldo hasta %ds",
                settings.PRICE_CACHE_TTL_S,
                settings.PRICE_CACHE_STALE_MAX_S,
            )

        return cls(providers)

    def get(self, slug: str) -> PriceProvider | None:
        return self._providers.get(slug)

    def all(self) -> list[PriceProvider]:
        return list(self._providers.values())

    def chains(self) -> list[Chain]:
        return [provider.chain for provider in self._providers.values()]

    async def aclose(self) -> None:
        await asyncio.gather(
            *(provider.aclose() for provider in self._providers.values()),
            return_exceptions=True,
        )
