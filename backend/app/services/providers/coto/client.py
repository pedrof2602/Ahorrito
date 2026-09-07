"""Cliente HTTP de la API de Constructor.io que sirve el catálogo de Coto.

Devuelve JSON crudo: traducir es tarea del mapper.

Se apoya en `app.services.http` para rate limiting, reintentos y taxonomía de
errores, igual que el cliente VTEX. Lo único propio de Constructor.io es la
forma de la URL (`/search/{term}` con la clave en el query string) y que la
respuesta útil viene anidada bajo `response`.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlencode

import httpx

from app.services.http import (
    AsyncRateLimiter,
    ProviderBadRequest,
    ProviderError,
    ProviderUnavailable,
    build_client,
    request_json,
)

from .config import CotoConfig

logger = logging.getLogger(__name__)

_SEARCH_PATH = "/search/{term}"
_BROWSE_PATH = "/browse/group_id/{group_id}"


@dataclass(slots=True)
class Page:
    """Una página de resultados y lo que sabemos de la paginación."""

    items: list[dict[str, Any]] = field(default_factory=list)
    page: int = 1
    total: int | None = None
    groups: list[dict[str, Any]] = field(default_factory=list)
    truncated: bool = False
    """True cuando cortamos por `max_pages` y quedó catálogo sin leer."""


class CotoClient:
    """Cliente de la API de Constructor.io de Coto."""

    def __init__(
        self,
        config: CotoConfig,
        *,
        user_agent: str,
        max_retries: int = 3,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.config = config
        self._max_retries = max_retries
        self._owns_client = client is None
        self._client = client or build_client(
            config.base_url,
            user_agent=user_agent,
            timeout_s=config.timeout_s,
            max_concurrency=config.max_concurrency,
        )
        self._limiter = AsyncRateLimiter(config.max_concurrency, config.min_interval_ms)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> CotoClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    def _params(self, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"key": self.config.api_key}
        params.update(extra or {})
        return {k: v for k, v in params.items() if v is not None}

    async def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        """GET que devuelve el bloque `response` ya desenvuelto.

        `quote_via=quote` deja los espacios como `%20`. No está medido que
        Constructor.io rechace `+` —el WAF que lo hacía es el de VTEX—, pero
        `%20` es lo que manda el propio sitio y no cuesta nada emitir lo mismo.
        """
        url = f"{path}?{urlencode(params, quote_via=quote)}"
        body, _ = await request_json(
            self._client,
            "GET",
            url,
            limiter=self._limiter,
            max_retries=self._max_retries,
        )
        if not isinstance(body, dict):
            raise ProviderError(
                f"{self.config.slug}: se esperaba un objeto JSON, "
                f"llegó {type(body).__name__}"
            )
        response = body.get("response")
        if not isinstance(response, dict):
            raise ProviderError(
                f"{self.config.slug}: la respuesta no trae el bloque 'response'"
            )
        return response

    @staticmethod
    def _page_from(response: dict[str, Any], page: int) -> Page:
        results = response.get("results")
        total = response.get("total_num_results")
        groups = response.get("groups")
        return Page(
            items=[r for r in results if isinstance(r, dict)]
            if isinstance(results, list)
            else [],
            page=page,
            total=total if isinstance(total, int) else None,
            groups=[g for g in groups if isinstance(g, dict)]
            if isinstance(groups, list)
            else [],
        )

    async def search_page(
        self,
        term: str,
        *,
        page: int = 1,
        page_size: int = 50,
        groups_max_depth: int | None = None,
    ) -> Page:
        """Una página de búsqueda por texto.

        Sirve también para buscar por EAN: Constructor.io indexa el código de
        barras como término, y `/search/7790742333605` devuelve exactamente ese
        producto (medido: `total_num_results` = 1).
        """
        extra: dict[str, Any] = {
            "page": page,
            "num_results_per_page": min(page_size, self.config.max_page_size),
        }
        if groups_max_depth is not None:
            extra["fmt_options[groups_max_depth]"] = groups_max_depth
        response = await self._get(
            _SEARCH_PATH.format(term=quote(term, safe="")), self._params(extra)
        )
        return self._page_from(response, page)

    async def browse_page(
        self, group_id: str, *, page: int = 1, page_size: int = 50
    ) -> Page:
        """Una página de una categoría (`group_id` de Constructor.io)."""
        response = await self._get(
            _BROWSE_PATH.format(group_id=quote(group_id, safe="")),
            self._params(
                {
                    "page": page,
                    "num_results_per_page": min(page_size, self.config.max_page_size),
                }
            ),
        )
        return self._page_from(response, page)

    async def iter_group_pages(
        self, group_id: str, *, page_size: int | None = None
    ) -> AsyncIterator[Page]:
        """Pagina una categoría entera.

        Corta por página vacía, por haber leído el total declarado, o por
        `max_pages`. En este último caso emite una página con `truncated=True`:
        perder catálogo en silencio es el peor modo de falla de un comparador.
        """
        size = min(page_size or self.config.max_page_size, self.config.max_page_size)
        collected = 0
        for page_number in range(1, self.config.max_pages + 1):
            page = await self.browse_page(group_id, page=page_number, page_size=size)
            if not page.items:
                return
            yield page
            collected += len(page.items)
            if page.total is not None and collected >= page.total:
                return
        yield Page(page=self.config.max_pages, truncated=True)

    async def search(
        self, term: str, *, limit: int = 50
    ) -> tuple[list[dict[str, Any]], int | None]:
        """Búsqueda por término. Devuelve `(resultados crudos, total declarado)`."""
        page = await self.search_page(term, page=1, page_size=limit)
        return page.items[:limit], page.total

    async def category_groups(self, depth: int = 3) -> list[dict[str, Any]]:
        """Árbol de categorías crudo.

        Constructor.io no tiene endpoint de árbol: lo publica como faceta de
        grupos de una búsqueda. Se pide sobre un término amplio y con
        `groups_max_depth`, que por defecto viene en 1 y devolvería sólo el
        primer nivel.
        """
        page = await self.search_page(
            "a", page=1, page_size=1, groups_max_depth=depth
        )
        return page.groups

    async def safe_search(
        self, term: str, *, limit: int = 50
    ) -> tuple[list[dict[str, Any]], int | None]:
        """Como `search`, pero devuelve vacío en lugar de propagar el fallo.

        Existe para los recorridos donde una cadena caída no debe cortar el
        trabajo (ingesta, comparación de canasta). **La búsqueda federada no la
        usa**: `search_all` ya aísla el fallo por cadena y lo informa en
        `errors`, y tragarlo acá lo disfrazaría de "no hay resultados", que es
        justo la confusión que el modelo de errores existe para evitar.
        """
        try:
            return await self.search(term, limit=limit)
        except (ProviderError, ProviderUnavailable, ProviderBadRequest) as exc:
            logger.warning("%s: búsqueda de %r falló: %s", self.config.slug, term, exc)
            return [], None
