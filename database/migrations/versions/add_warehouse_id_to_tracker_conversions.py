"""Add warehouse_id to order_tracker, conversion_batches, and conversions

Revision ID: add_warehouse_id_to_tracker_conversions
Revises: add_warehouse_support
Create Date: 2026-03-19 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'add_warehouse_id'
down_revision = 'add_warehouse_support'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── 1. Add warehouse_id to order_tracker ──────────────────────────────────
    op.add_column(
        'order_tracker',
        sa.Column('warehouse_id', sa.Integer(), nullable=True, server_default='1'),
    )
    op.execute("UPDATE order_tracker SET warehouse_id = 1 WHERE warehouse_id IS NULL")
    op.alter_column('order_tracker', 'warehouse_id', nullable=False, server_default=None)
    op.create_foreign_key(
        'fk_order_tracker_warehouse_id',
        'order_tracker', 'warehouses',
        ['warehouse_id'], ['id'],
    )

    # ── 2. Add warehouse_id to conversion_batches ─────────────────────────────
    op.add_column(
        'conversion_batches',
        sa.Column('warehouse_id', sa.Integer(), nullable=True, server_default='1'),
    )
    op.execute("UPDATE conversion_batches SET warehouse_id = 1 WHERE warehouse_id IS NULL")
    op.alter_column('conversion_batches', 'warehouse_id', nullable=False, server_default=None)
    op.create_foreign_key(
        'fk_conversion_batches_warehouse_id',
        'conversion_batches', 'warehouses',
        ['warehouse_id'], ['id'],
    )

    # ── 3. Add warehouse_id to conversions ────────────────────────────────────
    op.add_column(
        'conversions',
        sa.Column('warehouse_id', sa.Integer(), nullable=True, server_default='1'),
    )
    op.execute("UPDATE conversions SET warehouse_id = 1 WHERE warehouse_id IS NULL")
    op.alter_column('conversions', 'warehouse_id', nullable=False, server_default=None)
    op.create_foreign_key(
        'fk_conversions_warehouse_id',
        'conversions', 'warehouses',
        ['warehouse_id'], ['id'],
    )


def downgrade() -> None:
    op.drop_constraint('fk_conversions_warehouse_id', 'conversions', type_='foreignkey')
    op.drop_column('conversions', 'warehouse_id')

    op.drop_constraint('fk_conversion_batches_warehouse_id', 'conversion_batches', type_='foreignkey')
    op.drop_column('conversion_batches', 'warehouse_id')

    op.drop_constraint('fk_order_tracker_warehouse_id', 'order_tracker', type_='foreignkey')
    op.drop_column('order_tracker', 'warehouse_id')
