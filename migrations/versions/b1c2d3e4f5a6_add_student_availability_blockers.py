"""add student availability blockers

- calendar_blocks.blocks_auto_invitations (bool, default False)
- calendar_block_type enum gains value 'unavailable' (student-facing blocker)

Students create CalendarBlock rows with type='unavailable' and
blocks_auto_invitations=True to suppress AUTOMATIC class invitations during
their unavailable windows. Manual additions by a coach are unaffected.

Revision ID: b1c2d3e4f5a6
Revises: 7a4c9e2d1f30
Create Date: 2026-07-03 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b1c2d3e4f5a6'
down_revision = '7a4c9e2d1f30'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()

    # 1. Extend the calendar_block_type enum with 'unavailable'.
    #    On PG, ALTER TYPE ... ADD VALUE cannot run inside a transaction block,
    #    so use IF NOT EXISTS and autocommit. On SQLite the enum is just a
    #    CHECK-less VARCHAR, so nothing to do.
    if bind.dialect.name == 'postgresql':
        with op.get_context().autocommit_block():
            op.execute(
                "ALTER TYPE calendar_block_type ADD VALUE IF NOT EXISTS 'unavailable'"
            )

    # 2. Add the blocks_auto_invitations column.
    with op.batch_alter_table('calendar_blocks', schema=None) as batch_op:
        batch_op.add_column(sa.Column(
            'blocks_auto_invitations',
            sa.Boolean(),
            server_default='0',
            nullable=False,
        ))


def downgrade():
    with op.batch_alter_table('calendar_blocks', schema=None) as batch_op:
        batch_op.drop_column('blocks_auto_invitations')
    # Note: removing a value from a PG enum is not supported by ALTER TYPE;
    # the 'unavailable' value is left in place on downgrade (harmless).
