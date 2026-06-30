"""Add no_stock_deduction flags to order_items and products

Revision ID: add_no_stock_deduction_flags
Revises: add_order_calendar_events
Create Date: 2026-06-29 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = "add_no_stock_deduction_flags"
down_revision = "add_order_calendar_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "order_items",
        sa.Column("no_stock_deduction", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.add_column(
        "products",
        sa.Column("default_no_stock_deduction", sa.Boolean(), nullable=False, server_default="false"),
    )

    # Migrate legacy Media_Cut lines to Product_Item + flag
    op.execute(
        """
        UPDATE order_items
        SET no_stock_deduction = true, type = 'Product_Item'
        WHERE type = 'Media_Cut'
        """
    )

    # Default media catalog products to no-stock-deduction preset
    op.execute(
        """
        UPDATE products
        SET default_no_stock_deduction = true
        WHERE category_id = 4
        """
    )


def downgrade() -> None:
    op.drop_column("products", "default_no_stock_deduction")
    op.drop_column("order_items", "no_stock_deduction")
