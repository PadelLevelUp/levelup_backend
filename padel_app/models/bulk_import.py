from sqlalchemy import Column, Integer, String, ForeignKey, DateTime, Enum, Text
from sqlalchemy.orm import relationship
from datetime import datetime

from padel_app.sql_db import db
from padel_app import model


class BulkImport(db.Model, model.Model):
    __tablename__ = "bulk_imports"
    __table_args__ = {"extend_existing": True}

    page_title = "Bulk Imports"
    model_name = "BulkImport"

    id = Column(Integer, primary_key=True)
    coach_id = Column(Integer, ForeignKey("coaches.id", ondelete="CASCADE"), nullable=False)
    filename = Column(String(255), nullable=True)
    status = Column(
        Enum("active", "reverted", name="bulk_import_status"),
        default="active",
        nullable=False,
    )
    # JSON-encoded summary: {"Players": 2, "Classes": 1}
    summary = Column(Text, nullable=True)
    # JSON-encoded record IDs for revert: {"users": [1,2], "players": [3,4], ...}
    record_ids = Column(Text, nullable=True)

    coach = relationship("Coach")

    @property
    def name(self):
        return f"Import #{self.id}"

    @classmethod
    def get_create_form(cls):
        from padel_app.tools.input_tools import Block, Field, Form
        form = Form()
        form.add_block(Block("info_block", fields=[
            Field(instance_id=cls.id, model=cls.model_name, name="coach", label="Coach", type="ManyToOne", related_model="Coach"),
        ]))
        return form
