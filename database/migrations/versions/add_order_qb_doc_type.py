"""Add orders.qb_doc_type and scoped external_order_number uniqueness

Revision ID: add_order_qb_doc_type
Revises: add_force_no_stock_perm
Create Date: 2026-08-10 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = "add_order_qb_doc_type"
down_revision = "add_force_no_stock_perm"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("orders", sa.Column("qb_doc_type", sa.String(), nullable=True))

    # Backfill: incoming -> purchase_order; numeric QB# > 2000 -> invoice; else sales_order
    # Skip void orders and rows without an external reference.
    op.execute(
        """
        UPDATE orders
        SET qb_doc_type = CASE
            WHEN type = 'void' THEN NULL
            WHEN external_order_number IS NULL OR BTRIM(external_order_number) = '' THEN NULL
            WHEN type = 'incoming' THEN 'purchase_order'
            WHEN external_order_number ~ '^[0-9]+$'
                 AND external_order_number::bigint > 2000 THEN 'invoice'
            ELSE 'sales_order'
        END
        """
    )

    op.create_index(
        "uq_orders_external_qb_doc",
        "orders",
        ["external_order_number", "qb_doc_type"],
        unique=True,
        postgresql_where=sa.text(
            "external_order_number IS NOT NULL AND qb_doc_type IS NOT NULL"
        ),
    )


def downgrade() -> None:
    op.drop_index("uq_orders_external_qb_doc", table_name="orders")
    op.drop_column("orders", "qb_doc_type")
