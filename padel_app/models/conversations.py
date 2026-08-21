from datetime import datetime
from sqlalchemy import Column, Integer, String, Boolean, DateTime
from sqlalchemy.orm import relationship
from padel_app.sql_db import db
from padel_app import model
from padel_app.tools.input_tools import Block, Field, Form


class Conversation(db.Model, model.Model):
    __tablename__ = "conversations"
    __table_args__ = {"extend_existing": True}
    page_title = "Conversations"
    model_name = "Conversation"

    id = Column(Integer, primary_key=True)
    group_name = Column(String, nullable=True)
    is_group = Column(Boolean, default=False, nullable=False)
    
    participant_key =Column(
        String(255),
        nullable=False,
        unique=True,
        index=True,
    )

    messages = relationship(
        "Message",
        back_populates="conversation",
        cascade="all, delete-orphan",
    )

    participants = relationship(
        "ConversationParticipant",
        back_populates="conversation",
        cascade="all, delete-orphan",
    )
    
    @property
    def name(self):
        return f"Conversation {self.id}"
    
    def last_read_by(self, user_id):
        # PAD-125: match on `user_id`, not `p.id`. `p` is a
        # ConversationParticipant, so `p.id` is the join row's primary key —
        # a different sequence from `users.id`. Comparing it against a user id
        # matched no row (every message then serialized as unread) or, worse,
        # matched another participant's row and leaked their read state.
        participant = next(
            (p for p in self.participants if p.user_id == user_id),
            None
        )
        return participant.last_read_at if participant else None
    
    @staticmethod
    def build_participant_key(participant_ids: list[int]) -> str:
        return ",".join(map(str, sorted(set(participant_ids))))

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
                get_field("group_name", "Group name", "Text"),
                get_field("is_group", "Is group", "Boolean"),
                # PAD-93: there is no `validated` column on Conversation. The
                # phantom field made `get_edit_form()` raise AttributeError
                # (it does `getattr(self, field.name)` for every field) and
                # `update_with_dict` set a stray instance attribute on create.
                # Removed.
                get_field("participant_key", "Participant key", "Text"),
            ],
        )
        form.add_block(info_block)
        
        return form
