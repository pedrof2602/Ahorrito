"""tokens personales para el atajo de siri

Crea `api_tokens` y borra las tres tablas de la integración con Alexa, que se
descartó: `alexa_links` era del flujo de Login with Amazon —para la API de listas
que Amazon apagó el 1 de julio de 2024— y `alexa_skill_codes` / `alexa_skill_tokens`
del account linking del skill propio, que quedó sin usar.

**El drop borra vínculos reales**, no tablas vacías: en producción había una fila
en `alexa_skill_tokens`. No se migra a `api_tokens` a propósito. Son credenciales
de sistemas distintos —una la tenía Amazon, la otra la va a tener un atajo en el
iPhone— y arrastrar el hash de una para que valga como la otra sería darle acceso
nuevo a algo que ya no existe. Quien usaba Alexa emite un token desde la pantalla
de configuración.

Revision ID: 1775f6ce2669
Revises: a0c4a031b85a
Create Date: 2026-09-08 21:18:38.127491

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '1775f6ce2669'
down_revision: Union[str, Sequence[str], None] = 'a0c4a031b85a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('api_tokens',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('token_hash', sa.String(length=64), nullable=False),
    sa.Column('name', sa.String(length=60), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('api_tokens', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_api_tokens_token_hash'), ['token_hash'], unique=True)
        batch_op.create_index(batch_op.f('ix_api_tokens_user_id'), ['user_id'], unique=False)

    with op.batch_alter_table('alexa_skill_tokens', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_alexa_skill_tokens_access_token_hash'))
        batch_op.drop_index(batch_op.f('ix_alexa_skill_tokens_expires_at'))
        batch_op.drop_index(batch_op.f('ix_alexa_skill_tokens_refresh_token_hash'))
        batch_op.drop_index(batch_op.f('ix_alexa_skill_tokens_user_id'))

    op.drop_table('alexa_skill_tokens')
    with op.batch_alter_table('alexa_skill_codes', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_alexa_skill_codes_code_hash'))
        batch_op.drop_index(batch_op.f('ix_alexa_skill_codes_expires_at'))
        batch_op.drop_index(batch_op.f('ix_alexa_skill_codes_user_id'))

    op.drop_table('alexa_skill_codes')
    with op.batch_alter_table('alexa_links', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_alexa_links_user_id'))

    op.drop_table('alexa_links')


def downgrade() -> None:
    """Downgrade schema."""
    op.create_table('alexa_links',
    sa.Column('id', sa.INTEGER(), nullable=False),
    sa.Column('user_id', sa.INTEGER(), nullable=False),
    sa.Column('access_token_enc', sa.TEXT(), nullable=False),
    sa.Column('refresh_token_enc', sa.TEXT(), nullable=False),
    sa.Column('expires_at', sa.DATETIME(), nullable=False),
    sa.Column('scope', sa.VARCHAR(length=200), nullable=False),
    sa.Column('linked_at', sa.DATETIME(), nullable=False),
    sa.Column('refreshed_at', sa.DATETIME(), nullable=True),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('alexa_links', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_alexa_links_user_id'), ['user_id'], unique=1)

    op.create_table('alexa_skill_codes',
    sa.Column('id', sa.INTEGER(), nullable=False),
    sa.Column('code_hash', sa.VARCHAR(length=64), nullable=False),
    sa.Column('user_id', sa.INTEGER(), nullable=False),
    sa.Column('code_challenge', sa.VARCHAR(length=128), nullable=True),
    sa.Column('redirect_uri', sa.VARCHAR(length=400), nullable=False),
    sa.Column('expires_at', sa.DATETIME(), nullable=False),
    sa.Column('created_at', sa.DATETIME(), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('alexa_skill_codes', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_alexa_skill_codes_user_id'), ['user_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_alexa_skill_codes_expires_at'), ['expires_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_alexa_skill_codes_code_hash'), ['code_hash'], unique=1)

    op.create_table('alexa_skill_tokens',
    sa.Column('id', sa.INTEGER(), nullable=False),
    sa.Column('user_id', sa.INTEGER(), nullable=False),
    sa.Column('access_token_hash', sa.VARCHAR(length=64), nullable=False),
    sa.Column('refresh_token_hash', sa.VARCHAR(length=64), nullable=False),
    sa.Column('expires_at', sa.DATETIME(), nullable=False),
    sa.Column('created_at', sa.DATETIME(), nullable=False),
    sa.Column('last_used_at', sa.DATETIME(), nullable=True),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('alexa_skill_tokens', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_alexa_skill_tokens_user_id'), ['user_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_alexa_skill_tokens_refresh_token_hash'), ['refresh_token_hash'], unique=1)
        batch_op.create_index(batch_op.f('ix_alexa_skill_tokens_expires_at'), ['expires_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_alexa_skill_tokens_access_token_hash'), ['access_token_hash'], unique=1)

    with op.batch_alter_table('api_tokens', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_api_tokens_user_id'))
        batch_op.drop_index(batch_op.f('ix_api_tokens_token_hash'))

    op.drop_table('api_tokens')
