"""Cliente HTTP genérico para inquilinos VTEX.

Conoce el protocolo VTEX y nada de una cadena en particular: todo lo específico
entra por `VTEXStoreConfig`. Devuelve JSON crudo; traducir es tarea del mapper.

Se apoya en `catalog_system` como fuente primaria. Intelligent Search quedó
descartada tras medir su cobertura: en Carrefour devuelve 0 resultados para
'arroz', 'aceite', 'fideos' y 'azucar', donde legacy devuelve entre 500 y 750.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlencode

import httpx

from app.services.http import (
    AsyncRateLimiter,
    ProviderBadRequest,
    ProviderError,
    build_client,
    request_json,
)

from .config import VTEXStoreConfig
from .quirks import parse_resources

logger = logging.getLogger(__name__)

_SEARCH_PATH = "/api/catalog_system/pub/products/search/"
_CATEGORY_TREE_PATH = "/api/catalog_system/pub/category/tree/{depth}"
_REGIONS_PATH = "/api/checkout/pub/regions"
_SIMULATION_PATH = "/api/checkout/pub/orderForms/simulation"

_PROBE_PAYMENT_SYSTEM = "4"
"""Medio de pago que acompaña al BIN en la simulación.

**El campo es obligatorio, su valor es indiferente.** Medido contra Carrefour: un
`paymentData` con `bin` pero sin `paymentSystem` no activa ninguna promo (el
precio vuelve al de góndola), mientras que `1` (Amex), `2` (Visa) y `4`
(Mastercard) con el mismo BIN activan las tres el mismo descuento. Quien matchea
es el BIN; el `paymentSystem` solo tiene que estar presente para que el motor de
promociones corra.
"""

MIN_BIN_LENGTH = 6
MAX_BIN_LENGTH = 8
"""Un BIN de menos de 6 dígitos rompe la simulación: devuelve la respuesta sin
`items`, que es indistinguible de "la sucursal no vende eso". Medido con `5078`."""


def encode_params(params: dict[str, Any]) -> str:
    """Serializa los query params con los espacios como `%20`, no como `+`.

    El WAF de VTEX rechaza `+` con `400 Bad Request! Scripts are not allowed!`,
    y httpx codifica con `+` por defecto. Sin esto, cualquier búsqueda de más de
    una palabra ("dulce de leche", "aceite girasol") falla en ambas cadenas.
    """
    flat: list[tuple[str, str]] = []
    for key, value in params.items():
        if value is None:
            continue
        values = value if isinstance(value, (list, tuple)) else [value]
        flat.extend((key, str(item)) for item in values)
    return urlencode(flat, quote_via=_quote_vtex)


def _quote_vtex(string: str, safe: str = "", encoding: str | None = None,
                errors: str | None = None) -> str:
    """Deja `/` y `:` literales, como los manda el propio sitio.

    Importa en los filtros de categoría (`fq=C:/3/4/5/`): escaparlos a
    `C%3A%2F3%2F` es un cambio innecesario sobre la forma que sabemos que anda.
    """
    return quote(string, safe="/:", encoding=encoding, errors=errors)


@dataclass(slots=True)
class Page:
    """Una página de resultados y lo que sabemos de la paginación."""

    items: list[dict[str, Any]]
    offset: int
    total: int | None
    exhausted: bool = False
    truncated: bool = False
    """True cuando cortamos por el tope de offset y quedó catálogo sin leer."""


class VTEXClient:
    """Cliente de una tienda VTEX. Uno por cadena."""

    def __init__(
        self,
        config: VTEXStoreConfig,
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

    async def __aenter__(self) -> VTEXClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def _get(
        self, path: str, params: dict[str, Any] | None = None
    ) -> tuple[Any, httpx.Response]:
        # La query se arma a mano en lugar de pasarla por `params=`: httpx
        # codificaría los espacios como `+` y el WAF de VTEX lo rechaza.
        url = f"{path}?{encode_params(params)}" if params else path
        return await request_json(
            self._client,
            "GET",
            url,
            limiter=self._limiter,
            max_retries=self._max_retries,
        )

    def _regional_params(self, sales_channel: int | None) -> dict[str, Any]:
        """Params de regionalización según lo que soporte la cadena.

        En Disco mandar `sc` rompe la respuesta ("sc is inactive"), así que la
        estrategia NONE simplemente no lo emite.
        """
        resolved, _ = self.config.resolve_price_scope(sales_channel)
        return {"sc": resolved} if resolved is not None else {}

    async def _fetch_page(
        self, base_params: dict[str, Any], offset: int, size: int
    ) -> Page:
        params = {**base_params, "_from": offset, "_to": offset + size - 1}
        body, response = await self._get(_SEARCH_PATH, params)
        if not isinstance(body, list):
            raise ProviderError(
                f"{self.config.slug}: se esperaba una lista de productos, "
                f"llegó {type(body).__name__}"
            )
        resources = parse_resources(response.headers.get("resources"))
        return Page(
            items=body,
            offset=offset,
            total=resources[2] if resources else None,
            # 206 = página parcial, hay más. 200 = esto era todo.
            exhausted=response.status_code == 200 or not body,
        )

    async def iter_search_pages(
        self,
        *,
        term: str | None = None,
        category_fq: str | None = None,
        extra_fq: list[str] | None = None,
        sales_channel: int | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[Page]:
        """Pagina `catalog_system` respetando los topes del protocolo.

        Corta por: HTTP 200, página vacía, `limit` alcanzado, o tope de offset.
        La última marca `truncated=True` para que el llamador sepa que quedó
        catálogo sin leer en lugar de creer que terminó.
        """
        config = self.config
        base_params: dict[str, Any] = {**self._regional_params(sales_channel)}
        if term:
            base_params["ft"] = term
        fq_values = list(extra_fq or [])
        if category_fq:
            fq_values.insert(0, category_fq)
        if fq_values:
            base_params["fq"] = fq_values

        offset = 0
        collected = 0
        while offset <= config.max_offset:
            size = config.max_page_size
            if limit is not None:
                size = min(size, limit - collected)
                if size <= 0:
                    return

            try:
                page = await self._fetch_page(base_params, offset, size)
            except ProviderBadRequest as exc:
                # Un 400 más allá de la primera página es el techo de offset de
                # VTEX: no es reintentable y es el final de lo alcanzable.
                # En la primera página, en cambio, significa que la consulta
                # está mal armada (sales channel inactivo, fq inválido) y hay
                # que dejarlo salir: tragarlo devolvería "sin resultados" y
                # esconde el bug detrás de una respuesta plausible.
                if offset == 0:
                    raise
                logger.warning(
                    "%s: corte de paginación en offset %d (%s)",
                    config.slug,
                    offset,
                    exc,
                )
                yield Page(items=[], offset=offset, total=None, truncated=True)
                return

            if page.items:
                yield page
                collected += len(page.items)
            if page.exhausted or (limit is not None and collected >= limit):
                return
            offset += len(page.items)

        # Salimos por el tope de offset con catálogo todavía disponible.
        yield Page(items=[], offset=offset, total=None, truncated=True)

    async def search(
        self,
        term: str,
        *,
        sales_channel: int | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Búsqueda por término. Devuelve productos crudos."""
        products: list[dict[str, Any]] = []
        async for page in self.iter_search_pages(
            term=term, sales_channel=sales_channel, limit=limit
        ):
            products.extend(page.items)
        return products[:limit]

    async def category_tree(self, depth: int = 3) -> list[dict[str, Any]]:
        body, _ = await self._get(_CATEGORY_TREE_PATH.format(depth=depth))
        return body if isinstance(body, list) else []

    async def regions(self, postal_code: str) -> list[dict[str, Any]]:
        """Sucursales que sirven a un código postal.

        Solo tiene sentido si `supports_region_discovery`; en Disco devuelve 500.
        """
        if not self.config.supports_region_discovery:
            return []
        body, _ = await self._get(
            _REGIONS_PATH,
            {"country": self.config.country, "postalCode": postal_code},
        )
        return body if isinstance(body, list) else []

    async def simulate(
        self,
        sku_quantities: list[tuple[str, int]],
        *,
        seller_id: str = "1",
        postal_code: str | None = None,
        sales_channel: int | None = None,
        card_bin: str | None = None,
    ) -> dict[str, Any]:
        """Precio autoritativo vía checkout.

        Es lo que realmente pagarías: aplica las promociones de la sucursal y
        devuelve los importes ya en centavos. Más caro que la búsqueda, así que
        se usa para confirmar una canasta, no para poblar el catálogo.

        Con `card_bin` aplica además las promos condicionadas al medio de pago.
        **Ese descuento se pregunta acá, nunca se calcula.** Las promos de VTEX no
        se acumulan —el motor elige una por ítem—, así que multiplicar el total
        por el porcentaje del teaser exagera el ahorro: medido sobre una canasta
        de tres productos, el ahorro real de "Tarjeta Carrefour 15%" fue $1.499
        contra los $3.991 que da la cuenta ingenua, porque en un ítem ya corría un
        30% mejor y en otro la tarjeta desplazó a la promo que estaba aplicada.
        """
        payload: dict[str, Any] = {
            "items": [
                {"id": sku_id, "quantity": qty, "seller": seller_id}
                for sku_id, qty in sku_quantities
            ],
            "country": self.config.country,
        }
        if postal_code:
            payload["postalCode"] = postal_code
        if card_bin:
            payload["paymentData"] = {
                "payments": [
                    {
                        "paymentSystem": _PROBE_PAYMENT_SYSTEM,
                        "bin": card_bin,
                        "installments": 1,
                        "installmentsInterestRate": 0,
                        "referenceValue": 0,
                        "value": 0,
                    }
                ]
            }

        params = self._regional_params(sales_channel)
        url = f"{_SIMULATION_PATH}?{encode_params(params)}" if params else _SIMULATION_PATH
        body, _ = await request_json(
            self._client,
            "POST",
            url,
            json=payload,
            limiter=self._limiter,
            max_retries=self._max_retries,
        )
        return body if isinstance(body, dict) else {}
