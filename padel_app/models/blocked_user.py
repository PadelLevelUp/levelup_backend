from sqlalchemy import Column, Integer, ForeignKey, UniqueConstraint
from sqlalchemy.orm import relationship
from padel_app.sql_db import db
from padel_app import model


class BlockedUser(db.Model, model.Model):
    __tablename__ = "blocked_users"
    __table_args__ = (
        UniqueConstraint("blocker_id", "blocked_id", name="uq_blocked_user"),
        {"extend_existing": True},
    )

    id         = Column(Integer, primary_key=True)
    blocker_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    blocked_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)

    blocker = relationship("User", foreign_keys=[blocker_id])
    blocked = relationship("User", foreign_keys=[blocked_id])
