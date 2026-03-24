"""Add multi-warehouse support: warehouses table, warehouse_id on quantities/orders/transactions

Revision ID: add_warehouse_support
Revises: add_media_tables
Create Date: 2026-03-16 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'add_warehouse_support'
down_revision = 'add_media_tables'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── 1. Create warehouses table ────────────────────────────────────────────
    op.create_table(
        'warehouses',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('name', sa.String(100), nullable=False),
        sa.Column('address', sa.String(255), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default='true'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name'),
    )

    # ── 2. Insert the default "Main Warehouse" so existing rows can reference it
    op.execute(
        "INSERT INTO warehouses (id, name, is_active) VALUES (1, 'Main Warehouse', true)"
    )

    # ── 3. Add warehouse_id to quantities ─────────────────────────────────────
    op.add_column(
        'quantities',
        sa.Column('warehouse_id', sa.Integer(), nullable=True),
    )
    op.execute("UPDATE quantities SET warehouse_id = 1 WHERE warehouse_id IS NULL")
    op.alter_column('quantities', 'warehouse_id', nullable=False)
    op.create_foreign_key(
        'fk_quantities_warehouse_id',
        'quantities', 'warehouses',
        ['warehouse_id'], ['id'],
    )
    op.create_unique_constraint(
        'uq_quantity_product_warehouse',
        'quantities',
        ['product_id', 'warehouse_id'],
    )

    # ── 4. Add warehouse_id to orders ─────────────────────────────────────────
    op.add_column(
        'orders',
        sa.Column('warehouse_id', sa.Integer(), nullable=True, server_default='1'),
    )
    op.execute("UPDATE orders SET warehouse_id = 1 WHERE warehouse_id IS NULL")
    op.alter_column('orders', 'warehouse_id', nullable=False, server_default=None)
    op.create_foreign_key(
        'fk_orders_warehouse_id',
        'orders', 'warehouses',
        ['warehouse_id'], ['id'],
    )

    # ── 5. Add warehouse_id to transactions ───────────────────────────────────
    op.add_column(
        'transactions',
        sa.Column('warehouse_id', sa.Integer(), nullable=True, server_default='1'),
    )
    op.execute("UPDATE transactions SET warehouse_id = 1 WHERE warehouse_id IS NULL")
    op.alter_column('transactions', 'warehouse_id', nullable=False, server_default=None)
    op.create_foreign_key(
        'fk_transactions_warehouse_id',
        'transactions', 'warehouses',
        ['warehouse_id'], ['id'],
    )


def downgrade() -> None:
    # Remove foreign keys and columns in reverse order
    op.drop_constraint('fk_transactions_warehouse_id', 'transactions', type_='foreignkey')
    op.drop_column('transactions', 'warehouse_id')

    op.drop_constraint('fk_orders_warehouse_id', 'orders', type_='foreignkey')
    op.drop_column('orders', 'warehouse_id')

    op.drop_constraint('uq_quantity_product_warehouse', 'quantities', type_='unique')
    op.drop_constraint('fk_quantities_warehouse_id', 'quantities', type_='foreignkey')
    op.drop_column('quantities', 'warehouse_id')

    op.drop_table('warehouses')
