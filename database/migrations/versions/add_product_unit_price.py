"""Add products.unit_price

Revision ID: add_product_unit_price
Revises: add_order_qb_doc_type
Create Date: 2026-09-24 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = "add_product_unit_price"
down_revision = "add_order_qb_doc_type"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "products",
        sa.Column("unit_price", sa.Float(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("products", "unit_price")
