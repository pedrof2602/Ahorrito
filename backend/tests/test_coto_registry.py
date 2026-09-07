"""Alta de Coto en el registro de proveedores.

Coto entra por una rama distinta de la de VTEX —no es un inquilino más, habla
otra API—, así que lo que se prueba es que la convivencia y el interruptor de
`ENABLED_CHAINS` sigan valiendo para las dos ramas por igual.
"""

from __future__ import annotations

import asyncio

import pytest

from app.core.config import settings
from app.services.providers.cache import CachedProvider
from app.services.providers.coto.provider import CotoProvider
from app.services.providers.registry import ProviderRegistry


@pytest.fixture
def registry_factory():
    """Construye registros y los cierra al terminar.

    El registro abre un cliente httpx por cadena; dejarlos colgando ensucia el
    resto de la suite con warnings de sockets sin cerrar.
    """
    built: list[ProviderRegistry] = []

    def build(enabled, *, cache: bool | None = None):
        registry = ProviderRegistry.build(enabled, cache=cache)
        built.append(registry)
        return registry

    yield build

    for registry in built:
        asyncio.run(registry.aclose())


def _unwrap(provider):
    """Lo que hay debajo de la caché de precios.

    El registro envuelve cada proveedor en un `CachedProvider`; estos tests
    miran qué cadena quedó adentro, que es lo que el envoltorio no cambia.
    """
    return getattr(provider, "_inner", provider)


def test_coto_queda_registrado_por_defecto(registry_factory):
    """Lista vacía en ENABLED_CHAINS = todas las cadenas registradas."""
    registry = registry_factory([])
    assert registry.get("coto-ar") is not None
    assert "coto-ar" in {c.slug for c in registry.chains()}


def test_se_puede_habilitar_solo_coto(registry_factory):
    registry = registry_factory(["coto-ar"])
    assert [c.slug for c in registry.chains()] == ["coto-ar"]


def test_se_puede_deshabilitar_coto(registry_factory):
    registry = registry_factory(["carrefour-ar"])
    assert registry.get("coto-ar") is None


def test_convive_con_las_cadenas_vtex(registry_factory):
    registry = registry_factory(["carrefour-ar", "disco-ar", "coto-ar"])
    assert {c.slug for c in registry.chains()} == {"carrefour-ar", "disco-ar", "coto-ar"}


def test_el_proveedor_registrado_es_el_de_coto(registry_factory):
    provider = registry_factory(["coto-ar"]).get("coto-ar")
    assert isinstance(_unwrap(provider), CotoProvider)


def test_toma_los_limites_http_comunes(registry_factory):
    """`HTTP_TIMEOUT_S` es la política de la app, no un default del proveedor."""
    provider = registry_factory(["coto-ar"]).get("coto-ar")
    assert isinstance(_unwrap(provider), CotoProvider)
    # Sin `_unwrap`: la caché delega lo que no envuelve, así que la config de la
    # cadena se sigue viendo a través de ella.
    assert provider.config.timeout_s == settings.HTTP_TIMEOUT_S
    assert provider.config.min_interval_ms == settings.HTTP_MIN_INTERVAL_MS


def test_la_cache_envuelve_tambien_a_las_cadenas_que_no_son_vtex(registry_factory):
    """Coto entra por otra rama del registro; el TTL no es de VTEX, es de la app."""
    provider = registry_factory(["coto-ar"], cache=True).get("coto-ar")
    assert isinstance(provider, CachedProvider)


def test_la_cache_se_puede_apagar(registry_factory):
    provider = registry_factory(["coto-ar"], cache=False).get("coto-ar")
    assert isinstance(provider, CotoProvider)


def test_la_cadena_declara_precios_por_sucursal(registry_factory):
    """Coto es la primera cadena soportada que puede dar precio de sucursal."""
    provider = registry_factory(["coto-ar"]).get("coto-ar")
    assert provider is not None
    assert provider.chain.supports_store_prices is True
