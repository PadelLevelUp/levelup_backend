from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, UniqueConstraint
from sqlalchemy.orm import relationship

from padel_app import model
from padel_app.sql_db import db


class WaitingListEntry(db.Model, model.Model):
    __tablename__ = "waiting_list_entries"
    __table_args__ = (
        UniqueConstraint("lesson_instance_id", "player_id", name="uq_waiting_session_player"),
        {"extend_existing": True},
    )

    page_title = "Waiting List Entry"
    model_name = "WaitingListEntry"

    id = Column(Integer, primary_key=True)

    lesson_instance_id = Column(
        Integer, ForeignKey("lesson_instances.id", ondelete="CASCADE"), nullable=False
    )
    player_id = Column(
        Integer, ForeignKey("players.id", ondelete="CASCADE"), nullable=False
    )
    coach_id = Column(
        Integer, ForeignKey("coaches.id", ondelete="CASCADE"), nullable=False
    )
    standing_entry_id = Column(
        Integer, ForeignKey("standing_waiting_list_entries.id", ondelete="SET NULL"), nullable=True
    )
    is_active = Column(Boolean, default=True, nullable=False)
    joined_at = Column(DateTime, default=datetime.utcnow)

    lesson_instance = relationship("LessonInstance")
    player = relationship("Player")
    coach = relationship("Coach")

    @property
    def name(self):
        return f"WaitingListEntry #{self.id}"
