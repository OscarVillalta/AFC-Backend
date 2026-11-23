"""Drop Google Calendar sync table and permissions

Revision ID: drop_order_calendar
Revises: add_price_manage_perm
Create Date: 2026-11-23 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = "drop_order_calendar"
down_revision = "add_price_manage_perm"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DELETE FROM role_permissions
        WHERE permission_id IN (
            SELECT id FROM permissions WHERE name IN ('calendar:view', 'calendar:manage')
        )
        """
    )
    op.execute(
        "DELETE FROM permissions WHERE name IN ('calendar:view', 'calendar:manage')"
    )
    op.drop_table("order_calendar_events")


def downgrade() -> None:
    op.create_table(
        "order_calendar_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("order_id", sa.Integer(), nullable=False),
        sa.Column("google_event_id", sa.String(), nullable=True),
        sa.Column("google_calendar_id", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("all_day", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("sync_status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("order_id"),
        sa.UniqueConstraint("google_event_id"),
    )
    op.execute(
        """
        INSERT INTO permissions (name, description)
        SELECT 'calendar:view', 'Allows action: calendar:view'
        WHERE NOT EXISTS (SELECT 1 FROM permissions WHERE name = 'calendar:view')
        """
    )
    op.execute(
        """
        INSERT INTO permissions (name, description)
        SELECT 'calendar:manage', 'Allows action: calendar:manage'
        WHERE NOT EXISTS (SELECT 1 FROM permissions WHERE name = 'calendar:manage')
        """
    )
