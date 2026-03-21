from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer
from sqlalchemy.orm import relationship

from padel_app import model
from padel_app.sql_db import db


class StandingWaitingListEntry(db.Model, model.Model):
    __tablename__ = "standing_waiting_list_entries"
    __table_args__ = {"extend_existing": True}

    page_title = "Standing Waiting List Entry"
    model_name = "StandingWaitingListEntry"

    id = Column(Integer, primary_key=True)
    coach_id = Column(
        Integer, ForeignKey("coaches.id", ondelete="CASCADE"), nullable=False
    )
    player_id = Column(
        Integer, ForeignKey("players.id", ondelete="CASCADE"), nullable=False
    )
    credits_total = Column(Integer, nullable=False)
    credits_used = Column(Integer, default=0, nullable=False)
    expires_at = Column(DateTime, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    coach = relationship("Coach")
    player = relationship("Player")

    @property
    def name(self):
        return f"StandingWaitingListEntry #{self.id}"
