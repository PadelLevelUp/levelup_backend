# Phase 5 — native push device-token storage.
# - Mirrors padel_app/models/push_subscriptions.py (the existing browser Web-Push
#   model): db.Model + model.Model mixin, created_at/updated_at come from model.Model.
# - Unlike PushSubscription (unique per user_id, one browser sub per user), a
#   DeviceToken is unique per *token* — a user can have several device tokens
#   (multiple phones/reinstalls), and re-registering an existing token reassigns
#   it to the new caller (upsert semantics live in the route, not here).
from sqlalchemy import Column, Integer, String, ForeignKey
from sqlalchemy.orm import relationship

from padel_app.sql_db import db
from padel_app import model
from padel_app.tools.input_tools import Block, Field, Form


class DeviceToken(db.Model, model.Model):
    __tablename__ = "device_tokens"
    __table_args__ = {"extend_existing": True}
    page_title = "Device Tokens"
    model_name = "DeviceToken"

    id = Column(Integer, primary_key=True)
    user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    token = Column(String(255), nullable=False, unique=True, index=True)
    platform = Column(String(32), nullable=True)

    user = relationship("User")

    @property
    def name(self):
        return f"Device token for user {self.user_id} ({self.platform})"

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
                get_field("token", "Token", "String", required=True),
                get_field("platform", "Platform", "String"),
            ],
        )
        form.add_block(info_block)
        return form
