"""Rename tracker department DELIVERY_DEPT to WAREHOUSE

Revision ID: rename_delivery_to_warehouse
Revises: remove_legacy_order_support
Create Date: 2026-07-08 00:00:00.000000

"""
from alembic import op


revision = "rename_delivery_to_warehouse"
down_revision = "remove_legacy_order_support"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE order_tracker
        SET current_department = 'WAREHOUSE'
        WHERE current_department = 'DELIVERY_DEPT'
        """
    )
    op.execute(
        """
        UPDATE order_history
        SET from_department = 'WAREHOUSE'
        WHERE from_department = 'DELIVERY_DEPT'
        """
    )
    op.execute(
        """
        UPDATE order_history
        SET to_department = 'WAREHOUSE'
        WHERE to_department = 'DELIVERY_DEPT'
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE order_tracker
        SET current_department = 'DELIVERY_DEPT'
        WHERE current_department = 'WAREHOUSE'
        """
    )
    op.execute(
        """
        UPDATE order_history
        SET from_department = 'DELIVERY_DEPT'
        WHERE from_department = 'WAREHOUSE'
        """
    )
    op.execute(
        """
        UPDATE order_history
        SET to_department = 'DELIVERY_DEPT'
        WHERE to_department = 'WAREHOUSE'
        """
    )
