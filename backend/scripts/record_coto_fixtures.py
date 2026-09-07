"""Graba respuestas reales de la API de Coto (Constructor.io) como fixtures.

Se graban recortadas: una respuesta de 50 productos pesa medio mega y lo que
hace falta para testear el mapeo son unos pocos casos elegidos. Cada caso se
selecciona por el rasgo que ejercita —promo por cantidad, descuento directo, la
sucursal 133 rota— y no por venir primero, así la fixture sigue probando lo que
se quiso probar aunque Coto reordene el catálogo.

Uso:  ./venv/bin/python scripts/record_coto_fixtures.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings
from app.services.providers.coto.client import CotoClient
from app.services.providers.coto.config import COTO_AR

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "coto"


def _has_conditional_discount(result: dict[str, Any]) -> bool:
    return any(d.get("takingText") for d in result["data"].get("discounts") or [])


def _has_direct_discount(result: dict[str, Any]) -> bool:
    discounts = result["data"].get("discounts") or []
    return bool(discounts) and not any(d.get("takingText") for d in discounts)


def _has_broken_store(result: dict[str, Any]) -> bool:
    """La sucursal 133 publica `formatPrice` roto; es el caso de cordura."""
    return any(p.get("store") == "133" for p in result["data"].get("price") or [])


def _no_discount(result: dict[str, Any]) -> bool:
    return not (result["data"].get("discounts") or [])


PICKERS = (
    ("promo por cantidad", _has_conditional_discount),
    ("descuento directo", _has_direct_discount),
    ("sucursal 133 rota", _has_broken_store),
    ("sin promo", _no_discount),
)


def _select(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    chosen: list[dict[str, Any]] = []
    seen: set[str] = set()
    for label, predicate in PICKERS:
        for result in results:
            key = result["data"].get("id")
            if key in seen or not predicate(result):
                continue
            seen.add(key)
            chosen.append(result)
            print(f"    + {label}: {result['data'].get('sku_display_name')}")
            break
        else:
            print(f"    ! sin caso para '{label}'")
    return chosen


async def main() -> None:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    client = CotoClient(COTO_AR, user_agent=settings.HTTP_USER_AGENT)

    try:
        for name, term in (("search_leche", "leche"), ("search_aceite", "aceite")):
            print(f"{name}: buscando {term!r}")
            page = await client.search_page(term, page=1, page_size=50)
            payload = {
                "response": {
                    "results": _select(page.items),
                    "total_num_results": page.total,
                    "groups": page.groups,
                }
            }
            (FIXTURES / f"{name}.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
            )

        print("search_ean: buscando 7790742333605")
        page = await client.search_page("7790742333605", page=1, page_size=5)
        (FIXTURES / "search_ean.json").write_text(
            json.dumps(
                {"response": {"results": page.items, "total_num_results": page.total}},
                ensure_ascii=False,
                indent=1,
            ),
            encoding="utf-8",
        )

        print("category_tree: faceta de grupos a profundidad 3")
        groups = await client.category_groups(3)
        (FIXTURES / "category_tree.json").write_text(
            json.dumps({"response": {"groups": groups}}, ensure_ascii=False, indent=1),
            encoding="utf-8",
        )

        print("search_sin_match: término inexistente (fallback semántico)")
        page = await client.search_page("zzzqqxnotaproduct", page=1, page_size=5)
        (FIXTURES / "search_sin_match.json").write_text(
            json.dumps(
                {"response": {"results": page.items, "total_num_results": page.total}},
                ensure_ascii=False,
                indent=1,
            ),
            encoding="utf-8",
        )
    finally:
        await client.aclose()

    print(f"\nFixtures escritas en {FIXTURES}")


if __name__ == "__main__":
    asyncio.run(main())
