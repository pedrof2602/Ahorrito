"""caché de búsquedas de precios

Revision ID: b41c7a9de205
Revises: f670931b07ad
Create Date: 2026-09-02 19:40:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b41c7a9de205'
down_revision: Union[str, Sequence[str], None] = 'f670931b07ad'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'price_search_cache',
        sa.Column('key', sa.String(length=64), nullable=False),
        sa.Column('chain_slug', sa.String(length=64), nullable=False),
        sa.Column('term', sa.String(length=300), nullable=False),
        sa.Column('sales_channel', sa.Integer(), nullable=True),
        sa.Column('store_key', sa.String(length=128), nullable=True),
        sa.Column('limit', sa.Integer(), nullable=False),
        sa.Column('offers', sa.JSON(), nullable=False),
        sa.Column('fetched_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('key'),
    )
    with op.batch_alter_table('price_search_cache', schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f('ix_price_search_cache_chain_slug'), ['chain_slug'], unique=False
        )
        batch_op.create_index(
            'ix_search_cache_purge', ['chain_slug', 'fetched_at'], unique=False
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('price_search_cache', schema=None) as batch_op:
        batch_op.drop_index('ix_search_cache_purge')
        batch_op.drop_index(batch_op.f('ix_price_search_cache_chain_slug'))

    op.drop_table('price_search_cache')
