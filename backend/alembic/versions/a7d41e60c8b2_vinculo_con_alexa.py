"""vinculo con alexa

Revision ID: a7d41e60c8b2
Revises: c3a71f4b92de
Create Date: 2026-09-08 00:00:00.000000

Crea `alexa_links`: los tokens de Login with Amazon de cada usuario que vinculó
su cuenta, cifrados.

No migra ni siembra nada. Antes de esto no había vínculos que rescatar, así que
la tabla nace vacía y se llena a medida que cada usuario pasa por el flujo de
autorización.

El índice único sobre `user_id` es lo único con contenido acá: es lo que hace que
volver a vincular sea un UPDATE y no una segunda fila con un token viejo que
nadie usa pero que sigue siendo válido contra Amazon.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a7d41e60c8b2'
down_revision: Union[str, Sequence[str], None] = 'c3a71f4b92de'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'alexa_links',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('access_token_enc', sa.Text(), nullable=False),
        sa.Column('refresh_token_enc', sa.Text(), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('scope', sa.String(length=200), nullable=False),
        sa.Column('linked_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('refreshed_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('alexa_links', schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f('ix_alexa_links_user_id'), ['user_id'], unique=True
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('alexa_links', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_alexa_links_user_id'))
    op.drop_table('alexa_links')
