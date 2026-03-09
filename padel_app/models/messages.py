from datetime import datetime
from sqlalchemy import Column, Integer, String, DateTime, Boolean, ForeignKey
from sqlalchemy.orm import relationship
from padel_app.sql_db import db
from padel_app import model
from padel_app.tools.input_tools import Block, Field, Form


class Message(db.Model, model.Model):
    __tablename__ = "messages"
    __table_args__ = {"extend_existing": True}

    page_title = "Message"
    model_name = "Message"

    id = Column(Integer, primary_key=True)
    text = Column(String, nullable=False)
    sent_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    
    # Relationships with User
    sender_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )

    sender = relationship(
        "User", foreign_keys=[sender_id], back_populates="messages_sent"
    )

    # Attachment
    attachment_id = Column(Integer, ForeignKey("images.id", ondelete="SET NULL"))
    attachment = relationship("Image", foreign_keys=[attachment_id])
    
    conversation_id = Column(
        Integer, ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    conversation = relationship(
        "Conversation", back_populates="messages"
    )

    # Reply-to (self-referential)
    reply_to_id = Column(Integer, ForeignKey("messages.id", ondelete="SET NULL"), nullable=True)
    reply_to    = relationship("Message", remote_side="Message.id", foreign_keys="Message.reply_to_id")

    edited     = Column(Boolean, default=False, nullable=False, server_default="false")
    is_deleted = Column(Boolean, default=False, nullable=False, server_default="false")

    reactions = relationship("MessageReaction", back_populates="message", cascade="all, delete-orphan")

    @property
    def attachment_url(self):
        return self.attachment.url() if self.attachment else None

    def display_all_info(self):
        searchable = {"field": "text", "label": "Message Text"}
        fields = [
            {"field": "sent_at", "label": "Sent At"},
            {"field": "sender_id", "label": "Sender"},
        ]
        return searchable, fields

    @classmethod
    def get_create_form(cls):
        def get_field(name, label, type, **kwargs):
            return Field(
                instance_id=cls.id,
                model=cls.model_name,
                name=name,
                label=label,
                type=type,
                **kwargs,
            )

        form = Form()

        picture_block = Block(
            "picture_block",
            fields=[get_field("attachment_id", "Attachment Image", "Picture")],
        )
        form.add_block(picture_block)

        info_block = Block(
            "info_block",
            fields=[
                get_field("text", "Text", "Text"),
                get_field("sent_at", "Sent At", "DateTime"),
                get_field("sender", "Sender", "ManyToOne", related_model="User"),
                get_field("conversation", "Conversation", "ManyToOne", related_model="Conversation"),
            ],
        )
        form.add_block(info_block)

        return form
