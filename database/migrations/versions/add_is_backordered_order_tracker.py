"""Add is_backordered to order_tracker

Revision ID: add_is_backordered_order_tracker
Revises: 88fc1e10983b
Create Date: 2026-03-09 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'add_is_backordered_order_tracker'
down_revision = '88fc1e10983b'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'order_tracker',
        sa.Column('is_backordered', sa.Boolean(), nullable=False, server_default='false'),
    )


def downgrade() -> None:
    op.drop_column('order_tracker', 'is_backordered')
