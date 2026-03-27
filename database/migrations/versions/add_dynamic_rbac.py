"""Add dynamic RBAC: roles, permissions, role_permissions tables; update users

Revision ID: add_dynamic_rbac
Revises: d1d0c43a1e27
Create Date: 2026-03-27 17:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'add_dynamic_rbac'
down_revision = 'd1d0c43a1e27'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create permissions table
    op.create_table(
        'permissions',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('description', sa.String(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name'),
    )

    # Create roles table
    op.create_table(
        'roles',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('description', sa.String(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name'),
    )

    # Create role_permissions association table
    op.create_table(
        'role_permissions',
        sa.Column('role_id', sa.Integer(), nullable=False),
        sa.Column('permission_id', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['role_id'], ['roles.id']),
        sa.ForeignKeyConstraint(['permission_id'], ['permissions.id']),
        sa.PrimaryKeyConstraint('role_id', 'permission_id'),
    )

    # Add role_id column to users (nullable initially for data migration)
    op.add_column('users', sa.Column('role_id', sa.Integer(), nullable=True))
    op.create_foreign_key('fk_users_role_id', 'users', 'roles', ['role_id'], ['id'])

    # --- Data migration: seed default roles and map existing users ---
    roles_table = sa.table('roles',
        sa.column('id', sa.Integer),
        sa.column('name', sa.String),
        sa.column('description', sa.String),
    )
    op.bulk_insert(roles_table, [
        {'id': 1, 'name': 'Admin', 'description': 'Administrator with full access'},
        {'id': 2, 'name': 'Warehouse', 'description': 'Warehouse staff'},
        {'id': 3, 'name': 'Sales', 'description': 'Sales team member'},
        {'id': 4, 'name': 'Service', 'description': 'Service team member'},
    ])

    # Map existing user role strings to role_id
    users_table = sa.table('users',
        sa.column('role', sa.String),
        sa.column('role_id', sa.Integer),
    )
    for role_name, role_id in [('Admin', 1), ('Warehouse', 2), ('Sales', 3), ('Service', 4)]:
        op.execute(
            users_table.update()
            .where(users_table.c.role == role_name)
            .values(role_id=role_id)
        )

    # Drop the old role string column
    op.drop_column('users', 'role')


def downgrade() -> None:
    # Re-add the old role string column
    op.add_column('users', sa.Column('role', sa.String(), nullable=False, server_default='Sales'))

    # Reverse data migration: map role_id back to role string
    users_table = sa.table('users',
        sa.column('role', sa.String),
        sa.column('role_id', sa.Integer),
    )
    for role_name, role_id in [('Admin', 1), ('Warehouse', 2), ('Sales', 3), ('Service', 4)]:
        op.execute(
            users_table.update()
            .where(users_table.c.role_id == role_id)
            .values(role=role_name)
        )

    op.drop_constraint('fk_users_role_id', 'users', type_='foreignkey')
    op.drop_column('users', 'role_id')

    op.drop_table('role_permissions')
    op.drop_table('roles')
    op.drop_table('permissions')
