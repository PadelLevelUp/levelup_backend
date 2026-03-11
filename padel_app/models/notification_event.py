from sqlalchemy import Column, Enum, ForeignKey, Integer
from sqlalchemy.orm import relationship

from padel_app.sql_db import db
from padel_app import model


class NotificationEvent(db.Model, model.Model):
    __tablename__ = "notification_events"
    __table_args__ = {"extend_existing": True}

    page_title = "Notification Event"
    model_name = "NotificationEvent"

    id = Column(Integer, primary_key=True)
    coach_id = Column(Integer, ForeignKey("coaches.id", ondelete="CASCADE"), nullable=False)
    lesson_instance_id = Column(Integer, ForeignKey("lesson_instances.id", ondelete="CASCADE"), nullable=False)
    player_id = Column(Integer, ForeignKey("players.id", ondelete="CASCADE"), nullable=False)
    type = Column(Enum("manual", "auto", name="notification_event_type"), default="manual", nullable=False)
    round_number = Column(Integer, default=1, nullable=False)
    status = Column(
        Enum("sent", "confirmed", "expired", "queued", name="notification_event_status"),
        default="sent",
        nullable=False,
    )
    # The conversation message that delivered this invite (nullable for older/auto events)
    message_id = Column(Integer, ForeignKey("messages.id", ondelete="SET NULL"), nullable=True)

    lesson_instance = relationship("LessonInstance")
    player = relationship("Player")
    coach = relationship("Coach")

    @property
    def name(self):
        return f"NotificationEvent #{self.id}"

    @classmethod
    def get_create_form(cls):
        from padel_app.tools.input_tools import Block, Field, Form
        form = Form()
        form.add_block(Block("info_block", fields=[
            Field(instance_id=cls.id, model=cls.model_name, name="coach", label="Coach", type="ManyToOne", related_model="Coach"),
            Field(instance_id=cls.id, model=cls.model_name, name="player", label="Player", type="ManyToOne", related_model="Player"),
        ]))
        return form
