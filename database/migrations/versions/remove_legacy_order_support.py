"""Remove legacy order type and order item type values

Revision ID: remove_legacy_order_support
Revises: add_no_stock_deduction_flags
Create Date: 2026-06-29 00:00:00.000000

"""
from alembic import op


revision = "remove_legacy_order_support"
down_revision = "add_no_stock_deduction_flags"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Legacy stored order type -> installation (default customer-facing type)
    op.execute(
        """
        UPDATE orders
        SET type = 'installation'
        WHERE type = 'outgoing'
        """
    )

    # Any remaining Media_Cut lines -> Product_Item + no_stock_deduction flag
    op.execute(
        """
        UPDATE order_items
        SET no_stock_deduction = true, type = 'Product_Item'
        WHERE type = 'Media_Cut'
        """
    )


def downgrade() -> None:
    pass
