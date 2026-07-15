"""Add order:forceNoStock permission

Revision ID: add_force_no_stock_perm
Revises: rename_delivery_to_warehouse
Create Date: 2026-07-14 00:00:00.000000

"""
from alembic import op


revision = "add_force_no_stock_perm"
down_revision = "rename_delivery_to_warehouse"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        INSERT INTO permissions (name, description)
        SELECT 'order:forceNoStock',
               'Force all order line items to no stock deduction and complete immediately'
        WHERE NOT EXISTS (
            SELECT 1 FROM permissions WHERE name = 'order:forceNoStock'
        )
        """
    )
    op.execute(
        """
        INSERT INTO role_permissions (role_id, permission_id)
        SELECT r.id, p.id
        FROM roles r
        CROSS JOIN permissions p
        WHERE r.name = 'Admin'
          AND p.name = 'order:forceNoStock'
          AND NOT EXISTS (
              SELECT 1 FROM role_permissions rp
              WHERE rp.role_id = r.id AND rp.permission_id = p.id
          )
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DELETE FROM role_permissions
        WHERE permission_id IN (
            SELECT id FROM permissions WHERE name = 'order:forceNoStock'
        )
        """
    )
    op.execute("DELETE FROM permissions WHERE name = 'order:forceNoStock'")
