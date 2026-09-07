"""Graba respuestas reales de VTEX como fixtures de test.

Los headers se graban junto al body porque la lógica de paginación depende de
`resources` y del status: escribirlos de memoria es exactamente cómo se cuela un
bug (el header real es `0-1/1566`, no `products 0-49/1566` como sugiere la doc).

Uso:  ./venv/bin/python scripts/record_fixtures.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from app.core.config import settings
from app.services.providers.vtex.client import encode_params
from app.services.providers.vtex.stores import CARREFOUR_AR, DISCO_AR

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "vtex"
KEEP_HEADERS = ("resources", "content-type")

# (config, nombre, método, path, params, body)
CASES = [
    (CARREFOUR_AR, "search_leche", "GET",
     "/api/catalog_system/pub/products/search/",
     {"ft": "leche", "_from": 0, "_to": 2}, None),
    (CARREFOUR_AR, "search_sc3", "GET",
     "/api/catalog_system/pub/products/search/",
     {"fq": "skuId:52726", "sc": 3}, None),
    (CARREFOUR_AR, "sc_inactive", "GET",
     "/api/catalog_system/pub/products/search/",
     {"fq": "skuId:52726", "sc": 2}, None),
    (CARREFOUR_AR, "regions_1425", "GET",
     "/api/checkout/pub/regions", {"country": "ARG", "postalCode": "1425"}, None),
    (CARREFOUR_AR, "category_tree", "GET",
     "/api/catalog_system/pub/category/tree/2", {}, None),
    (CARREFOUR_AR, "simulation", "POST",
     "/api/checkout/pub/orderForms/simulation", {"sc": 1},
     {"items": [{"id": "52726", "quantity": 1, "seller": "carrefourar0026"}],
      "country": "ARG", "postalCode": "1425"}),
    # El mismo SKU con y sin tarjeta. Grabar los dos es el punto: la fixture con
    # BIN sola no prueba nada —podría ser una promo general—, y lo que hay que
    # poder verificar es que el precio cambia *por* la tarjeta.
    (CARREFOUR_AR, "simulation_no_card", "POST",
     "/api/checkout/pub/orderForms/simulation", {"sc": 1},
     {"items": [{"id": "52726", "quantity": 1, "seller": "1"}],
      "country": "ARG", "postalCode": "1425"}),
    (CARREFOUR_AR, "simulation_bin", "POST",
     "/api/checkout/pub/orderForms/simulation", {"sc": 1},
     {"items": [{"id": "52726", "quantity": 1, "seller": "1"}],
      "country": "ARG", "postalCode": "1425",
      # `paymentSystem` es obligatorio aunque su valor sea indiferente: sin él
      # el motor de promociones no corre y vuelve el precio de góndola.
      "paymentData": {"payments": [{
          "paymentSystem": "4", "bin": "507858", "installments": 1,
          "installmentsInterestRate": 0, "referenceValue": 0, "value": 0,
      }]}}),
    (DISCO_AR, "search_leche", "GET",
     "/api/catalog_system/pub/products/search/",
     {"ft": "leche", "_from": 0, "_to": 2}, None),
    (DISCO_AR, "regions_error", "GET",
     "/api/checkout/pub/regions", {"country": "ARG", "postalCode": "1425"}, None),
    (DISCO_AR, "offset_too_deep", "GET",
     "/api/catalog_system/pub/products/search/",
     {"ft": "leche", "_from": 2600, "_to": 2609}, None),
]


async def main() -> None:
    async with httpx.AsyncClient(
        timeout=30.0,
        follow_redirects=True,
        headers={"User-Agent": settings.HTTP_USER_AGENT, "Accept": "application/json"},
    ) as client:
        for config, name, method, path, params, json_body in CASES:
            slug = config.slug.split("-")[0]
            out_dir = FIXTURES / slug
            out_dir.mkdir(parents=True, exist_ok=True)

            url = config.base_url + path
            if params:
                url = f"{url}?{encode_params(params)}"
            response = await client.request(method, url, json=json_body)

            (out_dir / f"{name}.json").write_text(
                response.text, encoding="utf-8"
            )
            meta = {
                "status_code": response.status_code,
                "headers": {
                    k: v for k, v in response.headers.items() if k.lower() in KEEP_HEADERS
                },
            }
            (out_dir / f"{name}.meta.json").write_text(
                json.dumps(meta, indent=2), encoding="utf-8"
            )
            print(f"  {slug}/{name}: HTTP {response.status_code}, {len(response.text)} bytes")
            await asyncio.sleep(0.4)


if __name__ == "__main__":
    asyncio.run(main())
