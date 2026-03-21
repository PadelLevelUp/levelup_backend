from datetime import datetime

from sqlalchemy import Column, DateTime, Enum, ForeignKey, Integer
from sqlalchemy.orm import relationship

from padel_app import model
from padel_app.sql_db import db


class Vacancy(db.Model, model.Model):
    __tablename__ = "vacancies"
    __table_args__ = {"extend_existing": True}

    page_title = "Vacancy"
    model_name = "Vacancy"

    id = Column(Integer, primary_key=True)

    lesson_instance_id = Column(
        Integer, ForeignKey("lesson_instances.id", ondelete="CASCADE"), nullable=False
    )
    coach_id = Column(
        Integer, ForeignKey("coaches.id", ondelete="CASCADE"), nullable=False
    )
    # The player who vacated the spot (None for structurally open spots)
    original_player_id = Column(
        Integer, ForeignKey("players.id", ondelete="SET NULL"), nullable=True
    )
    # Snapshotted from the departing player's Association_CoachPlayer at creation time
    side = Column(Enum("left", "right", name="vacancy_side"), nullable=True)
    level_id = Column(Integer, ForeignKey("coach_levels.id"), nullable=True)

    status = Column(
        Enum("open", "filled", "expired", name="vacancy_status"),
        default="open",
        nullable=False,
    )
    current_round_number = Column(Integer, default=1, nullable=False)
    current_batch_number = Column(Integer, default=0, nullable=False)

    filled_by_player_id = Column(
        Integer, ForeignKey("players.id", ondelete="SET NULL"), nullable=True
    )
    # Updated on every send or response; used to determine maxInactiveTime
    last_activity_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    filled_at = Column(DateTime, nullable=True)

    lesson_instance = relationship("LessonInstance")
    coach = relationship("Coach")
    original_player = relationship("Player", foreign_keys=[original_player_id])
    filled_by_player = relationship("Player", foreign_keys=[filled_by_player_id])
    level = relationship("CoachLevel")
    notification_events = relationship("NotificationEvent", back_populates="vacancy")

    @property
    def name(self):
        return f"Vacancy #{self.id} for {self.lesson_instance}"
