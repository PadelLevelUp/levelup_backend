"""add seasons and recurs_until_season_end

Revision ID: c9d1e2f3a4b5
Revises: b1c2d3e4f5a6
Create Date: 2026-07-05 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c9d1e2f3a4b5'
down_revision = 'b1c2d3e4f5a6'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'seasons',
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('coach_id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=120), nullable=False),
        sa.Column('start_date', sa.Date(), nullable=False),
        sa.Column('end_date', sa.Date(), nullable=False),
        sa.ForeignKeyConstraint(['coach_id'], ['coaches.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.add_column(
        'lessons',
        sa.Column(
            'recurs_until_season_end',
            sa.Boolean(),
            nullable=False,
            server_default='0',
        ),
    )


def downgrade():
    op.drop_column('lessons', 'recurs_until_season_end')
    op.drop_table('seasons')
