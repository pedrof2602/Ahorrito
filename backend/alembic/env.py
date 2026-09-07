"""Entorno de Alembic.

Dos cosas lo separan de la plantilla por defecto:

- La URL sale de `app.core.db`, no del `alembic.ini`. Tenerla en el .ini
  significaría mantener la misma cadena de conexión en dos lugares, y el día que
  se mueva a Postgres uno de los dos se olvida.
- `render_as_batch` en SQLite: SQLite casi no soporta `ALTER TABLE`, y sin modo
  batch cualquier migración que cambie una columna falla. Alembic lo emula
  recreando la tabla y copiando los datos.
"""

import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context
from app.core.db import DATABASE_URL
from app.db.tables import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", DATABASE_URL)

target_metadata = Base.metadata


def _is_sqlite() -> bool:
    return DATABASE_URL.startswith("sqlite")


def run_migrations_offline() -> None:
    """Genera el SQL sin conectarse a nada (`alembic upgrade head --sql`)."""
    context.configure(
        url=DATABASE_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=_is_sqlite(),
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=_is_sqlite(),
        # Sin esto, un cambio de largo en un String() no se detecta y la
        # migración sale vacía sin avisar.
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
