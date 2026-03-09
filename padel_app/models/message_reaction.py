from datetime import datetime
from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.orm import relationship
from padel_app.sql_db import db
from padel_app import model


class MessageReaction(db.Model, model.Model):
    __tablename__ = "message_reactions"
    __table_args__ = (
        UniqueConstraint("message_id", "user_id", "emoji", name="uq_reaction"),
        {"extend_existing": True},
    )

    id         = Column(Integer, primary_key=True)
    message_id = Column(Integer, ForeignKey("messages.id", ondelete="CASCADE"), nullable=False)
    user_id    = Column(Integer, ForeignKey("users.id",    ondelete="CASCADE"), nullable=False)
    emoji      = Column(String(8), nullable=False)

    message = relationship("Message", back_populates="reactions")
    user    = relationship("User", foreign_keys=[user_id])
