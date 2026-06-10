from sqlalchemy import Column, Integer, String, ForeignKey, Enum, DateTime
from sqlalchemy.orm import relationship

from padel_app.sql_db import db
from padel_app import model
from padel_app.tools.input_tools import Block, Field, Form


class CoachInvitation(db.Model, model.Model):
    __tablename__ = "coach_invitations"
    __table_args__ = {"extend_existing": True}

    page_title = "Coach Invitations"
    model_name = "CoachInvitation"

    id = Column(Integer, primary_key=True)

    club_id = Column(
        Integer, ForeignKey("clubs.id", ondelete="CASCADE"), nullable=False
    )
    club = relationship("Club")

    token = Column(String(64), unique=True, nullable=False, index=True)
    email = Column(String(120), nullable=True)

    invited_by_coach_id = Column(Integer, ForeignKey("coaches.id"))
    invited_by_coach = relationship("Coach")

    status = Column(
        Enum(
            "pending",
            "accepted",
            "revoked",
            "expired",
            name="coach_invitation_status",
        ),
        nullable=False,
        server_default="pending",
    )
    expires_at = Column(DateTime, nullable=False)

    @property
    def name(self):
        return f"Invitation to {self.club.name} ({self.status})"

    def __repr__(self):
        return f"<CoachInvitation club={self.club_id} status={self.status}>"

    def __str__(self):
        return self.name

    @property
    def display_name(self):
        return str(self)

    @classmethod
    def display_all_info(cls):
        searchable = {"field": "token", "label": "Token"}
        columns = [
            {"field": "club", "label": "Club"},
            {"field": "email", "label": "Email"},
            {"field": "status", "label": "Status"},
            {"field": "expires_at", "label": "Expires At"},
        ]
        return searchable, columns

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
                get_field("club", "ManyToOne", label="Club", related_model="Club"),
                get_field("token", "Text", label="Token"),
                get_field("email", "Text", label="Email"),
                get_field(
                    "invited_by_coach",
                    "ManyToOne",
                    label="Invited by",
                    related_model="Coach",
                ),
                get_field(
                    "status",
                    "Select",
                    label="Status",
                    options=["pending", "accepted", "revoked", "expired"],
                ),
                get_field("expires_at", "DateTime", label="Expires At"),
            ],
        )
        form.add_block(info_block)

        return form
