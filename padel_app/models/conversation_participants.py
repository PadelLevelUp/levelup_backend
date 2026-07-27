from sqlalchemy import Column, Integer, DateTime, ForeignKey
from sqlalchemy.orm import relationship
from datetime import datetime
from padel_app.sql_db import db
from padel_app import model
from padel_app.tools.input_tools import Block, Field, Form


class ConversationParticipant(db.Model, model.Model):
    __tablename__ = "conversation_participants"
    __table_args__ = {"extend_existing": True}
    page_title = "Conversation Partipants"
    model_name = "ConversationParticipant"

    id = Column(Integer, primary_key=True)

    conversation_id = Column(
        Integer,
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
    )

    user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )

    joined_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    last_read_at = Column(DateTime, nullable=True)

    conversation = relationship("Conversation", back_populates="participants")
    user = relationship("User")
    
    @property
    def name(self):
        return f"Link between {self.conversation} and {self.user}"
    
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
                get_field("user", "User", "Text"),
                get_field("conversation", "Conversation", "Text"),
                # PAD-93: these are DateTime columns, not booleans. They were
                # declared as "Boolean" (with copy-pasted labels from
                # Conversation), so the generic editor rendered them as
                # checkboxes and every save wrote `False` into a timestamp
                # column — and `get_edit_form()` fed a datetime back into a
                # Boolean field. Declared with their real type now.
                get_field("joined_at", "Joined at", "DateTime"),
                get_field("last_read_at", "Last read at", "DateTime"),
            ],
        )
        form.add_block(info_block)
        
        return form
