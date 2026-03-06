"""add push subscriptions

Audit findings (Phase 1):
- Backend uses Flask-Migrate/Alembic migrations under migrations/versions.
- Current message flow is persisted before SSE publish in create_message_service().
- SSE connectivity is tracked globally in memory (not per user), so DB-backed push subscriptions
  are needed for closed-app delivery.

Revision ID: 5b9a1c2d7e8f
Revises: af4718b13e99
Create Date: 2026-03-06 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "5b9a1c2d7e8f"
down_revision = "af4718b13e99"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "push_subscriptions",
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("subscription_json", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id"),
    )
    with op.batch_alter_table("push_subscriptions", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_push_subscriptions_user_id"),
            ["user_id"],
            unique=True,
        )


def downgrade():
    with op.batch_alter_table("push_subscriptions", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_push_subscriptions_user_id"))
    op.drop_table("push_subscriptions")
