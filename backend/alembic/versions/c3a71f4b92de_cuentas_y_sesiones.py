"""cuentas y sesiones

Revision ID: c3a71f4b92de
Revises: b41c7a9de205
Create Date: 2026-09-07 00:00:00.000000

Crea `users` y `auth_sessions`, y —solo si la base ya venía con datos— reserva
el id 1 para la cuenta del dueño.

Ese detalle es todo el punto de la migración. Antes del login, cada domicilio,
tarjeta y lista se guardó con `user_id = 1` porque no había con qué distinguir
usuarios. Si la primera cuenta que se registrara tomara el id 2, todo eso
quedaría colgando de un usuario 1 que no existe: la app arrancaría vacía y los
datos seguirían ahí, invisibles. Sembrando la cuenta del dueño con id explícito
no se migra ni una fila de datos.

En una base recién creada no se siembra nada. Ahí no hay `user_id = 1` que
rescatar, y dejar una cuenta fantasma obligaría al primero que se registre a ser
el id 2 por una razón puramente histórica que en esa base nunca ocurrió.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c3a71f4b92de'
down_revision: Union[str, Sequence[str], None] = 'b41c7a9de205'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


OWNER_USER_ID = 1

UNUSABLE_PASSWORD = "!"
"""La cuenta del dueño nace sin contraseña usable.

No es un hash de Argon2 válido, así que ningún login la acepta. Ponerle una
contraseña por default acá sería peor de lo que parece: quedaría escrita en un
archivo versionado y pública para cualquiera que lea el repo. Se asigna con
`python -m scripts.set_password`."""

PLACEHOLDER_EMAIL = "dueno@localhost"
"""Cuando el perfil viejo no tenía email cargado. Es válido como identificador
—el índice único no se queja— y no pretende ser una casilla real: el dueño lo
cambia junto con la contraseña."""


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'users',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('email', sa.String(length=254), nullable=False),
        sa.Column('password_hash', sa.String(length=255), nullable=False),
        sa.Column('role', sa.String(length=16), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('last_login_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_users_email'), ['email'], unique=True)

    op.create_table(
        'auth_sessions',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('token_hash', sa.String(length=64), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('user_agent', sa.String(length=300), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('auth_sessions', schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f('ix_auth_sessions_user_id'), ['user_id'], unique=False
        )
        batch_op.create_index(
            batch_op.f('ix_auth_sessions_token_hash'), ['token_hash'], unique=True
        )
        batch_op.create_index(
            batch_op.f('ix_auth_sessions_expires_at'), ['expires_at'], unique=False
        )

    _seed_owner()


def _seed_owner() -> None:
    """Reserva el id 1 para el dueño, si la base ya tenía datos suyos."""
    bind = op.get_bind()

    # "¿Había datos antes del login?" se contesta mirando las tablas que cuelgan
    # de un usuario, no solo `user_profile`: el perfil se crea con defaults la
    # primera vez que alguien abre la pantalla de ajustes, así que puede existir
    # vacío en una base donde nunca se cargó nada.
    owned = sa.text(
        """
        SELECT
          (SELECT COUNT(*) FROM user_profile)        +
          (SELECT COUNT(*) FROM addresses)           +
          (SELECT COUNT(*) FROM payment_instruments) +
          (SELECT COUNT(*) FROM custom_stores)       +
          (SELECT COUNT(*) FROM shopping_lists)
        """
    )
    if not bind.execute(owned).scalar():
        return

    email = bind.execute(
        sa.text(
            "SELECT email FROM user_profile WHERE user_id = :uid AND email IS NOT NULL"
        ),
        {"uid": OWNER_USER_ID},
    ).scalar()

    bind.execute(
        sa.text(
            """
            INSERT INTO users (id, email, password_hash, role, is_active, created_at)
            VALUES (:id, :email, :pwd, 'user', 1, CURRENT_TIMESTAMP)
            """
        ),
        {
            "id": OWNER_USER_ID,
            "email": (email or PLACEHOLDER_EMAIL).strip().lower(),
            "pwd": UNUSABLE_PASSWORD,
        },
    )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('auth_sessions', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_auth_sessions_expires_at'))
        batch_op.drop_index(batch_op.f('ix_auth_sessions_token_hash'))
        batch_op.drop_index(batch_op.f('ix_auth_sessions_user_id'))
    op.drop_table('auth_sessions')

    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_users_email'))
    op.drop_table('users')
