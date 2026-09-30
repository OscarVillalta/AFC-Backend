"""Add price:manage permission

Revision ID: add_price_manage_perm
Revises: add_product_unit_price
Create Date: 2026-09-30 00:00:00.000000

"""
from alembic import op


revision = "add_price_manage_perm"
down_revision = "add_product_unit_price"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        INSERT INTO permissions (name, description)
        SELECT 'price:manage',
               'View and edit product unit prices'
        WHERE NOT EXISTS (
            SELECT 1 FROM permissions WHERE name = 'price:manage'
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
          AND p.name = 'price:manage'
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
            SELECT id FROM permissions WHERE name = 'price:manage'
        )
        """
    )
    op.execute("DELETE FROM permissions WHERE name = 'price:manage'")
