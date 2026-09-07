"""Motor async y sesiones de SQLAlchemy."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import BACKEND_DIR, settings
from app.db.tables import Base

DEFAULT_DB_PATH = BACKEND_DIR / "compras.db"
DATABASE_URL = settings.DATABASE_URL or f"sqlite+aiosqlite:///{DEFAULT_DB_PATH}"

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def _tune_sqlite(dbapi_connection, _record) -> None:
    """PRAGMAs necesarios para que SQLite aguante crawls concurrentes.

    WAL permite leer mientras se escribe (SQLite acepta un solo escritor), y el
    busy_timeout evita que un lector se caiga con "database is locked" mientras
    un crawl está insertando.
    """
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def get_engine(url: str | None = None) -> AsyncEngine:
    global _engine, _session_factory
    if _engine is None:
        _engine = create_async_engine(url or DATABASE_URL, future=True)
        if _engine.dialect.name == "sqlite":
            event.listen(_engine.sync_engine, "connect", _tune_sqlite)
        _session_factory = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    if _session_factory is None:
        get_engine()
    assert _session_factory is not None
    return _session_factory


async def create_all(engine: AsyncEngine | None = None) -> None:
    """Crea el esquema de cero. **Solo para tests.**

    En la app real el esquema lo maneja Alembic: `create_all` crea las tablas que
    faltan pero nunca altera una que ya existe, así que agregar una columna
    pasaría en silencio y reventaría en el primer query. Los tests sí lo usan
    —arrancan de una base en memoria vacía— porque es más rápido y no los ata a
    la cadena de migraciones.
    """
    target = engine or get_engine()
    async with target.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def _alembic_config():
    """Config de Alembic armada a mano, sin leer el `alembic.ini`.

    Leerlo traería su `[loggers]`, que pisa el `basicConfig` de la app y deja los
    logs en WARN. Sin `config_file_name`, `env.py` saltea el `fileConfig` y el
    logging de la app queda como estaba.
    """
    from alembic.config import Config

    config = Config()
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    config.set_main_option("sqlalchemy.url", DATABASE_URL)
    return config


async def upgrade_schema() -> None:
    """Deja la base en la última revisión.

    Va en un thread porque la API de Alembic es sincrónica y por dentro abre su
    propio event loop: llamarla desde el loop de FastAPI tira
    `asyncio.run() cannot be called from a running event loop`.
    """
    from alembic import command

    await asyncio.to_thread(command.upgrade, _alembic_config(), "head")


async def dispose_engine() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None


async def get_db() -> AsyncIterator[AsyncSession]:
    """Dependencia de FastAPI."""
    async with get_session_factory()() as session:
        yield session
