"""add late_cancellation to presences

Adds presences.late_cancellation (bool, default False). Set when a student
cancels their attendance at or after the coach's configured cancellation
deadline (but before the class starts). PAD-43.

Revision ID: c3d4e5f6a7b8
Revises: b1c2d3e4f5a6
Create Date: 2026-07-04 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c3d4e5f6a7b8'
down_revision = 'b1c2d3e4f5a6'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('presences', schema=None) as batch_op:
        batch_op.add_column(sa.Column(
            'late_cancellation',
            sa.Boolean(),
            server_default='false',
            nullable=False,
        ))


def downgrade():
    with op.batch_alter_table('presences', schema=None) as batch_op:
        batch_op.drop_column('late_cancellation')
