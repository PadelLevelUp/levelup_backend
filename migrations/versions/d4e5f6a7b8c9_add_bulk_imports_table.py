"""add_bulk_imports_table

Revision ID: d4e5f6a7b8c9
Revises: 3ff75d01a5ee
Create Date: 2026-04-02 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'd4e5f6a7b8c9'
down_revision = '3ff75d01a5ee'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('bulk_imports',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('coach_id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column('filename', sa.String(length=255), nullable=True),
        sa.Column('status', sa.Enum('active', 'reverted', name='bulk_import_status'), nullable=False, server_default='active'),
        sa.Column('updated_at', sa.DateTime(), nullable=True, server_default=sa.func.now()),
        sa.Column('summary', sa.Text(), nullable=True),
        sa.Column('record_ids', sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(['coach_id'], ['coaches.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade():
    op.drop_table('bulk_imports')
    op.execute("DROP TYPE IF EXISTS bulk_import_status")
