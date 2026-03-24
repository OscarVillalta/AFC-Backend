"""Add media_categories and media tables

Revision ID: add_media_tables
Revises: e90d0c5cb61d
Create Date: 2026-03-10 17:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'add_media_tables'
down_revision = 'add_is_backordered_order_tracker'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'media_categories',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('name', sa.String(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name'),
    )

    op.create_table(
        'media',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('part_number', sa.String(), nullable=False),
        sa.Column('description', sa.String(length=255), nullable=True),
        sa.Column('length', sa.Float(), nullable=True),
        sa.Column('width', sa.Float(), nullable=True),
        sa.Column('unit_of_measure', sa.String(length=50), nullable=True),
        sa.Column('category_id', sa.Integer(), nullable=False),
        sa.Column('supplier_id', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['category_id'], ['media_categories.id']),
        sa.ForeignKeyConstraint(['supplier_id'], ['suppliers.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('part_number'),
    )


def downgrade() -> None:
    op.drop_table('media')
    op.drop_table('media_categories')
