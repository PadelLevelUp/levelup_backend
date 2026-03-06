# Audit findings (Phase 1):
# - Message creation flow is /api/app/message -> create_message_service() in services/messaging_service.py.
# - SSE subscriptions are tracked globally in padel_app/realtime.py as an in-memory list of queues,
#   without user-scoped connection tracking.
# - ORM/DB stack is Flask-SQLAlchemy + SQLAlchemy models with Alembic migrations.
# - API auth uses flask-jwt-extended (@jwt_required + get_jwt_identity).
# - Env loading in development happens in levelup_backend/app.py via load_dotenv(".env.local.dev").
from sqlalchemy import Column, Integer, Text, ForeignKey
from sqlalchemy.orm import relationship

from padel_app.sql_db import db
from padel_app import model
from padel_app.tools.input_tools import Block, Field, Form


class PushSubscription(db.Model, model.Model):
    __tablename__ = "push_subscriptions"
    __table_args__ = {"extend_existing": True}
    page_title = "Push Subscriptions"
    model_name = "PushSubscription"

    id = Column(Integer, primary_key=True)
    user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    subscription_json = Column(Text, nullable=False)

    user = relationship("User")

    @property
    def name(self):
        return f"Push subscription for user {self.user_id}"

    @classmethod
    def get_create_form(cls):
        def get_field(name, label, type, required=False):
            return Field(
                instance_id=cls.id,
                model=cls.model_name,
                name=name,
                label=label,
                type=type,
                required=required,
            )

        form = Form()
        info_block = Block(
            "info_block",
            fields=[
                get_field("user", "User", "ManyToOne", required=True),
                get_field("subscription_json", "Subscription JSON", "Text", required=True),
            ],
        )
        form.add_block(info_block)
        return form
