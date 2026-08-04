"""PAD-112: student notification block preferences

Adds four columns to `users`:

- notif_block_auto_invitations   (bool, NOT NULL, default False)
- notif_block_manual_invitations (bool, NOT NULL, default False)
- notif_block_all                (bool, NOT NULL, default False)
- notif_block_reason             (text, nullable)

A student uses these to opt out of class-slot solicitations at three
independent levels, and to attach a free-text reason their coach can read.

Distinct from the PAD-28/PAD-107 availability blockers, which are rows in
`calendar_blocks` scoped to a time window. These are standing preferences with
no window at all.

The defaults are deliberately "receives everything", so every existing row keeps
today's behaviour: `server_default='0'` backfills the booleans in place and the
NOT NULL constraint is safe to add immediately.

Revision ID: c4d5e6f7a8b9
Revises: a7b8c9d0e1f2
Create Date: 2026-08-04 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c4d5e6f7a8b9'
down_revision = 'a7b8c9d0e1f2'
branch_labels = None
depends_on = None


_BOOL_COLUMNS = (
    'notif_block_auto_invitations',
    'notif_block_manual_invitations',
    'notif_block_all',
)


def _existing_columns(bind):
    return {col['name'] for col in sa.inspect(bind).get_columns('users')}


def upgrade():
    bind = op.get_bind()
    existing = _existing_columns(bind)

    with op.batch_alter_table('users') as batch_op:
        for name in _BOOL_COLUMNS:
            if name in existing:
                continue
            batch_op.add_column(
                sa.Column(
                    name,
                    sa.Boolean(),
                    nullable=False,
                    server_default=sa.text('false'),
                )
            )
        if 'notif_block_reason' not in existing:
            batch_op.add_column(
                sa.Column('notif_block_reason', sa.Text(), nullable=True)
            )


def downgrade():
    bind = op.get_bind()
    existing = _existing_columns(bind)

    with op.batch_alter_table('users') as batch_op:
        if 'notif_block_reason' in existing:
            batch_op.drop_column('notif_block_reason')
        for name in reversed(_BOOL_COLUMNS):
            if name in existing:
                batch_op.drop_column(name)
