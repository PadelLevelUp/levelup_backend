"""PAD-128: add notification_configs.eligibility_rules (nullable JSON)

The coach-tier standard eligibility bar (specs/eligibility/spec.md ->
eligibility.rules).

Nullable with NO server default, and existing rows are deliberately left at
NULL rather than being backfilled from their `invitation_groups`
(eligibility.rules rule 10). The shipped default invitation groups were never a
deliberate eligibility choice, and seeding them as a bar would permanently stop
a vacancy from ever widening past level+side — the exact fill behaviour
eligibility.enforcement rule 1 preserves. Day one after this ships, every
existing coach's bar is open and nothing about their invitation flow changes.

NULL must stay distinguishable from `[]` at the row level, so no default is
attached to the column.

Revision ID: d5e6f7a8b9c0
Revises: c4d5e6f7a8b9
Create Date: 2026-08-08

"""
from alembic import op
import sqlalchemy as sa


revision = "d5e6f7a8b9c0"
down_revision = "c4d5e6f7a8b9"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "notification_configs",
        sa.Column("eligibility_rules", sa.JSON(), nullable=True),
    )


def downgrade():
    op.drop_column("notification_configs", "eligibility_rules")
