"""skill de alexa

Las dos tablas del account linking, donde esta app es el **proveedor** OAuth y
Alexa el cliente: `alexa_skill_codes` para el código de un solo uso que dura
cinco minutos, y `alexa_skill_tokens` para lo que Alexa guarda después.

Convive con `alexa_links` (revisión a7d41e60c8b2), que es el flujo inverso —esta
app como cliente de Login with Amazon— y quedó sin uso cuando Amazon apagó la
List Management REST API el 1 de julio de 2024. Se dropea en otra revisión,
recién cuando el skill esté andando: mientras tanto sigue habiendo vínculos
guardados ahí y borrarlos antes de tener el reemplazo no le sirve a nadie.

Revision ID: a0c4a031b85a
Revises: a7d41e60c8b2
Create Date: 2026-09-08 15:42:57.705172

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a0c4a031b85a'
down_revision: Union[str, Sequence[str], None] = 'a7d41e60c8b2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('alexa_skill_codes',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('code_hash', sa.String(length=64), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('code_challenge', sa.String(length=128), nullable=True),
    sa.Column('redirect_uri', sa.String(length=400), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('alexa_skill_codes', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_alexa_skill_codes_code_hash'), ['code_hash'], unique=True)
        batch_op.create_index(batch_op.f('ix_alexa_skill_codes_expires_at'), ['expires_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_alexa_skill_codes_user_id'), ['user_id'], unique=False)

    op.create_table('alexa_skill_tokens',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('access_token_hash', sa.String(length=64), nullable=False),
    sa.Column('refresh_token_hash', sa.String(length=64), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('alexa_skill_tokens', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_alexa_skill_tokens_access_token_hash'), ['access_token_hash'], unique=True)
        batch_op.create_index(batch_op.f('ix_alexa_skill_tokens_expires_at'), ['expires_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_alexa_skill_tokens_refresh_token_hash'), ['refresh_token_hash'], unique=True)
        batch_op.create_index(batch_op.f('ix_alexa_skill_tokens_user_id'), ['user_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('alexa_skill_tokens', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_alexa_skill_tokens_user_id'))
        batch_op.drop_index(batch_op.f('ix_alexa_skill_tokens_refresh_token_hash'))
        batch_op.drop_index(batch_op.f('ix_alexa_skill_tokens_expires_at'))
        batch_op.drop_index(batch_op.f('ix_alexa_skill_tokens_access_token_hash'))

    op.drop_table('alexa_skill_tokens')
    with op.batch_alter_table('alexa_skill_codes', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_alexa_skill_codes_user_id'))
        batch_op.drop_index(batch_op.f('ix_alexa_skill_codes_expires_at'))
        batch_op.drop_index(batch_op.f('ix_alexa_skill_codes_code_hash'))

    op.drop_table('alexa_skill_codes')

