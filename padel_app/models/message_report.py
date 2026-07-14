from sqlalchemy import Column, Integer, String, ForeignKey
from sqlalchemy.orm import relationship
from padel_app.sql_db import db
from padel_app import model


class MessageReport(db.Model, model.Model):
    __tablename__ = "message_reports"
    __table_args__ = {"extend_existing": True}

    id          = Column(Integer, primary_key=True)
    reporter_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    message_id  = Column(Integer, ForeignKey("messages.id", ondelete="CASCADE"), nullable=False)
    reason      = Column(String, nullable=True)

    reporter = relationship("User", foreign_keys=[reporter_id])
    message  = relationship("Message", foreign_keys=[message_id])
