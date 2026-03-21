"""add updated_at to standing_waiting_list_entries

Revision ID: c2d3e4f5a6b7
Revises: b1d2e3f4a5c6
Create Date: 2026-03-21

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c2d3e4f5a6b7'
down_revision = 'b1d2e3f4a5c6'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'standing_waiting_list_entries',
        sa.Column('updated_at', sa.DateTime(), nullable=True),
    )


def downgrade():
    op.drop_column('standing_waiting_list_entries', 'updated_at')
