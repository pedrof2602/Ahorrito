from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

FIXTURES = Path(__file__).parent / "fixtures" / "vtex"


def load_fixture(chain: str, name: str) -> Any:
    return json.loads((FIXTURES / chain / f"{name}.json").read_text(encoding="utf-8"))


def load_meta(chain: str, name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / chain / f"{name}.meta.json").read_text(encoding="utf-8"))


@pytest.fixture
def carrefour_search() -> list[dict[str, Any]]:
    return load_fixture("carrefour", "search_leche")


@pytest.fixture
def disco_search() -> list[dict[str, Any]]:
    return load_fixture("disco", "search_leche")


@pytest.fixture
def carrefour_regions() -> list[dict[str, Any]]:
    return load_fixture("carrefour", "regions_1425")


@pytest.fixture
def carrefour_simulation() -> dict[str, Any]:
    return load_fixture("carrefour", "simulation")


@pytest.fixture
def carrefour_simulation_no_card() -> dict[str, Any]:
    """La misma canasta que `carrefour_simulation_bin`, sin tarjeta.

    Van de a pares: la simulación con BIN sola no prueba nada —el descuento
    podría ser una promo general— y lo que hay que poder verificar es que el
    precio cambia *por* la tarjeta."""
    return load_fixture("carrefour", "simulation_no_card")


@pytest.fixture
def carrefour_simulation_bin() -> dict[str, Any]:
    return load_fixture("carrefour", "simulation_bin")


@pytest.fixture
def carrefour_category_tree() -> list[dict[str, Any]]:
    return load_fixture("carrefour", "category_tree")


# --------------------------------------------------- base y sesión de usuario
#
# Vivían duplicadas en cada archivo de tests. Se centralizan acá porque desde el
# login son tres cosas acopladas —base, cuenta y cliente autenticado— y tenerlas
# repetidas hacía que agregar la cuenta al medio significara editar lo mismo en
# tres lugares.

import httpx
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import settings
from app.core.security import hash_password
from app.db.repositories import UserRepository
from app.db.tables import Base

TEST_PASSWORD = "una contraseña larga"


@pytest_asyncio.fixture
async def session():
    """Base en memoria, creada con `create_all` y no con Alembic: los tests
    arrancan de cero y no tienen por qué depender de la cadena de migraciones."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        yield s
    await engine.dispose()


@pytest_asyncio.fixture
async def user(session):
    """La cuenta dueña de todo lo que crean los tests.

    Se crea directo por el repositorio y no por `POST /auth/register`: los tests
    que la usan prueban perfiles, domicilios y sucursales, no el registro, y
    pasar por HTTP les metería un paso que puede fallar por motivos ajenos.
    """
    row = await UserRepository(session).create(
        "test@ejemplo.com", hash_password(TEST_PASSWORD)
    )
    await session.commit()
    return row


@pytest_asyncio.fixture
async def client(session, user, monkeypatch):
    """La app real, con la base de test y una sesión ya abierta.

    `COOKIE_SECURE=False` porque httpx no manda cookies `Secure` sobre
    `http://test`; sin eso, cada request daría 401 por un motivo que no tiene
    nada que ver con lo que el test quiere probar.

    Sin `LifespanManager` a propósito: el `lifespan` migraría la base de verdad
    y levantaría los clientes HTTP contra los supermercados.
    """
    monkeypatch.setattr(settings, "COOKIE_SECURE", False)

    from app.core.db import get_db
    from app.main import app

    app.dependency_overrides[get_db] = lambda: session
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        await c.post(
            "/api/v1/auth/login",
            json={"email": user.email, "password": TEST_PASSWORD},
        )
        yield c
    app.dependency_overrides.clear()
