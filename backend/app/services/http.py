"""Capa HTTP compartida por los proveedores de precios.

Concentra tres cosas que no queremos repetir por cadena: limitación de tasa,
política de reintentos y una taxonomía de errores que distinga "fallo
transitorio" de "estamos pidiendo mal".
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import time
from collections.abc import AsyncIterator, Mapping
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class ProviderError(Exception):
    """Error al hablar con el sitio de una cadena."""


class ProviderUnavailable(ProviderError):
    """Fallo transitorio: timeout, 5xx, corte de red. Reintentable."""


class ProviderRateLimited(ProviderUnavailable):
    """429. Reintentable, respetando Retry-After."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class ProviderBadRequest(ProviderError):
    """4xx que no es 429: pedimos mal. Reintentar solo esconde el bug."""


class ProviderBlocked(ProviderError):
    """Respondieron algo que no es JSON (challenge de bot, HTML de error)."""


class AsyncRateLimiter:
    """Limita concurrencia y espacia los requests a un mismo host.

    El semáforo acota cuántos requests hay en vuelo; el reloj monotónico impone
    una separación mínima entre salidas, que es lo que evita las ráfagas.
    """

    def __init__(self, max_concurrency: int, min_interval_ms: int) -> None:
        self._sem = asyncio.Semaphore(max_concurrency)
        self._interval = min_interval_ms / 1000
        self._lock = asyncio.Lock()
        self._next_at = 0.0

    @contextlib.asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        async with self._sem:
            async with self._lock:
                now = time.monotonic()
                wait = max(0.0, self._next_at - now)
                self._next_at = max(now, self._next_at) + self._interval
            if wait:
                await asyncio.sleep(wait)
            yield


def build_client(
    base_url: str,
    *,
    user_agent: str,
    timeout_s: float,
    max_concurrency: int,
    extra_headers: Mapping[str, str] | None = None,
) -> httpx.AsyncClient:
    """Crea un cliente por cadena.

    Uno por cadena, no uno global: el pool de conexiones queda aislado, así una
    cadena lenta no deja sin conexiones a la otra.

    Las cookies se desactivan a propósito. VTEX usa `vtex_segment` para fijar la
    región; si se persistiera entre requests, la región de una consulta
    contaminaría en silencio las siguientes. La regionalización va siempre
    explícita en los query params.
    """
    return httpx.AsyncClient(
        base_url=base_url,
        timeout=httpx.Timeout(timeout_s, connect=5.0),
        limits=httpx.Limits(
            max_connections=max_concurrency * 2,
            max_keepalive_connections=max_concurrency,
        ),
        headers={
            "User-Agent": user_agent,
            "Accept": "application/json",
            "Accept-Language": "es-AR,es;q=0.9",
            **(extra_headers or {}),
        },
        follow_redirects=True,
        cookies=None,
    )


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("retry-after")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def classify(response: httpx.Response) -> None:
    """Traduce el status a nuestra taxonomía. No-op si la respuesta sirve.

    206 es éxito: VTEX lo usa para páginas parciales.
    """
    if response.status_code < 400:
        return
    if response.status_code == 429:
        raise ProviderRateLimited(
            f"{response.request.url.host} limitó la tasa",
            retry_after=_retry_after(response),
        )
    if response.status_code >= 500:
        raise ProviderUnavailable(
            f"{response.request.url.host} devolvió {response.status_code}"
        )
    raise ProviderBadRequest(
        f"{response.status_code} en {response.request.url}: {response.text[:200]}"
    )


def parse_json(response: httpx.Response) -> Any:
    """Parsea el body defendiéndose de las respuestas raras de VTEX.

    VTEX devuelve HTTP 200 con un JSON *string* suelto para errores de sales
    channel (`"sc is inactive"`, `"sc not found"`). Sin esta guarda el mapper
    revienta con `TypeError: string indices must be integers` a varias capas de
    distancia de la causa real.
    """
    content_type = response.headers.get("content-type", "")
    if "json" not in content_type:
        raise ProviderBlocked(
            f"{response.request.url.host} respondió {content_type!r} en lugar de JSON"
        )
    try:
        body = response.json()
    except ValueError as exc:
        raise ProviderBlocked(f"JSON inválido de {response.request.url.host}") from exc

    if isinstance(body, str):
        raise ProviderBadRequest(f"La API respondió con el error: {body!r}")
    if isinstance(body, dict) and "error" in body:
        raise ProviderBadRequest(f"La API respondió con el error: {body['error']}")
    return body


async def request_json(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    limiter: AsyncRateLimiter,
    max_retries: int = 3,
    **kwargs: Any,
) -> tuple[Any, httpx.Response]:
    """Ejecuta un request con rate limiting y reintentos.

    Reintenta solo lo transitorio (5xx, 429, errores de transporte). Un 400
    nunca se reintenta: significa que el offset pasó de 2500 o que el `_to` es
    inválido, y martillarlo solo oculta el bug.
    """
    last_exc: Exception | None = None
    for attempt in range(max_retries):
        try:
            async with limiter.slot():
                response = await client.request(method, url, **kwargs)
            classify(response)
            return parse_json(response), response
        except (ProviderUnavailable, httpx.TransportError) as exc:
            last_exc = exc
            if attempt == max_retries - 1:
                break
            delay = getattr(exc, "retry_after", None) or (
                2**attempt * 0.5 + random.uniform(0, 0.3)
            )
            logger.warning(
                "Reintento %d/%d en %s tras %s (espera %.1fs)",
                attempt + 1,
                max_retries,
                url,
                type(exc).__name__,
                delay,
            )
            await asyncio.sleep(delay)

    assert last_exc is not None
    if isinstance(last_exc, httpx.TransportError):
        raise ProviderUnavailable(f"Error de transporte en {url}: {last_exc}") from last_exc
    raise last_exc
