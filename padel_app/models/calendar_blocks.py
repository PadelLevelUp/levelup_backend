from sqlalchemy import Column, Integer, String, ForeignKey, Enum, Boolean, Date, DateTime, Text
from sqlalchemy.orm import relationship
from padel_app.tools.input_tools import Block, Field, Form

from padel_app.sql_db import db
from padel_app import model

class CalendarBlock(db.Model, model.Model):
    __tablename__ = "calendar_blocks"
    
    page_title = "CalendarBlocks"
    model_name = "CalendarBlock"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"))
    user = relationship("User", back_populates="calendar_blocks")

    type = Column(Enum("break","holiday","off_work","personal","unavailable", name="calendar_block_type"), nullable=False)

    start_datetime = Column(DateTime, nullable=False)
    end_datetime = Column(DateTime, nullable=False)

    is_recurring = Column(Boolean, default=False, nullable=False)
    recurrence_rule = Column(Text, nullable=True)
    recurrence_end = Column(Date, nullable=True)

    # When True, this block suppresses AUTOMATIC class invitations for the owning
    # user during its window (recurring occurrences included). Manual additions by
    # a coach are unaffected. Used by the smart notification eligibility engine.
    blocks_auto_invitations = Column(Boolean, default=False, nullable=False, server_default="0")

    title = Column(String(255))
    description = Column(Text)
    
    @property
    def name(self):
        return f"<CalendarBlock {self.id}"
    
    @classmethod
    def get_create_form(cls):
        def get_field(name, type, label=None, **kwargs):
            return Field(
                instance_id=cls.id,
                model=cls.model_name,
                name=name,
                type=type,
                label=label or name.capitalize(),
                **kwargs,
            )

        form = Form()
        info_block = Block(
            "info_block",
            fields=[
                get_field("title", type="Text", label="Title"),
                get_field("description", type="Text", label="Description"),
                get_field("user", type="ManyToOne", label="User", related_model="User"),
                get_field("start_datetime", type="DateTime", label="Start Time"),
                get_field("end_datetime", type="DateTime", label="End Time"),
                get_field("is_recurring", type="Boolean", label="Is Recurring"),
                get_field("recurrence_rule", type="Text", label="Recurrence Rule"),
                get_field("recurrence_end", type="Date", label="Recurrence End"),
                get_field("blocks_auto_invitations", type="Boolean", label="Blocks Auto Invitations"),
                get_field("type", type="Select", label="Type", options=["break","holiday","off_work","personal","unavailable"]),
            ],
        )
        form.add_block(info_block)

        return form
