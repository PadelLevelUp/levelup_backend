from sqlalchemy import Boolean, Column, ForeignKey, Integer, JSON
from sqlalchemy.orm import relationship

from padel_app.sql_db import db
from padel_app import model


DEFAULT_PRIORITY_CRITERIA = [
    {"id": "level", "label": "Level", "enabled": True},
    {"id": "justified_misses", "label": "Justified Misses", "enabled": True},
    {"id": "attendance", "label": "Attendance", "enabled": True},
    {"id": "playing_side", "label": "Playing Side", "enabled": False},
    {"id": "subscription_status", "label": "Subscription Status", "enabled": False},
]

DEFAULT_RESTRICTIONS = {
    "maxSimultaneous": {"enabled": True, "value": 3},
    "maxTotal": {"enabled": True, "value": 10},
    "maxLevelDeviation": {"enabled": True, "value": 1},
    "minTimeBeforeClass": {"enabled": False, "value": 30},
    "maxInvitesPerStudentPerDay": {"enabled": False, "value": 3},
    "quietHours": {"enabled": False},
}

DEFAULT_ROUNDS = [
    {"id": 1, "duration": 10, "description": "Top-ranked students"},
    {"id": 2, "duration": 10, "description": "Next group"},
    {"id": 3, "duration": 15, "description": "Broader search"},
]

DEFAULT_NOTIFICATION_GROUPS = [
    {"id": "same_level", "label": "Same level", "enabled": True},
    {"id": "recent_absences", "label": "Recent absences", "enabled": True},
    {"id": "justified_absences", "label": "Justified absences", "enabled": True},
    {"id": "all_students", "label": "All students", "enabled": True},
]

DEFAULT_MESSAGE_TEMPLATES = {
    "invite": "Hey {name}, we have an opening in the {level} class next {weekday} at {time}. Do you want to come?",
    "confirm": "Great! I'm counting on you! See you there 🎾",
    "decline": "No problem, see you next time!",
    "spot_filled": "Sorry, this place was filled already! I'll get back to you if something else opens up.",
}


class NotificationConfig(db.Model, model.Model):
    __tablename__ = "notification_configs"
    __table_args__ = {"extend_existing": True}

    page_title = "Notification Config"
    model_name = "NotificationConfig"

    id = Column(Integer, primary_key=True)
    coach_id = Column(
        Integer, ForeignKey("coaches.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    auto_notify_enabled = Column(Boolean, default=False, nullable=False)
    priority_criteria = Column(JSON, nullable=True)
    restrictions = Column(JSON, nullable=True)
    rounds = Column(JSON, nullable=True)
    notification_groups = Column(JSON, nullable=True)
    message_templates = Column(JSON, nullable=True)

    coach = relationship("Coach")

    @property
    def name(self):
        return f"NotificationConfig for coach {self.coach_id}"

    def get_priority_criteria(self):
        return self.priority_criteria if self.priority_criteria is not None else DEFAULT_PRIORITY_CRITERIA

    def get_restrictions(self):
        return self.restrictions if self.restrictions is not None else DEFAULT_RESTRICTIONS

    def get_rounds(self):
        return self.rounds if self.rounds is not None else DEFAULT_ROUNDS

    def get_notification_groups(self):
        return self.notification_groups if self.notification_groups is not None else DEFAULT_NOTIFICATION_GROUPS

    def get_message_templates(self):
        if self.message_templates is None:
            return DEFAULT_MESSAGE_TEMPLATES
        # Fill missing keys with defaults
        return {**DEFAULT_MESSAGE_TEMPLATES, **self.message_templates}

    @classmethod
    def get_create_form(cls):
        from padel_app.tools.input_tools import Block, Field, Form
        form = Form()
        form.add_block(Block("info_block", fields=[
            Field(instance_id=cls.id, model=cls.model_name, name="coach", label="Coach", type="ManyToOne", related_model="Coach"),
        ]))
        return form
