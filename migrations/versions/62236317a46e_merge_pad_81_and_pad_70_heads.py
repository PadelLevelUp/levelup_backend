"""merge pad-81 and pad-70 heads

Revision ID: 62236317a46e
Revises: a1c2e3d4b5f6, d7e8f9a0b1c2
Create Date: 2026-07-23 16:40:34.591624

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '62236317a46e'
down_revision = ('a1c2e3d4b5f6', 'd7e8f9a0b1c2')
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
