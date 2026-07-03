from sqlalchemy import Column, Integer, String, ForeignKey, Enum, DateTime
from sqlalchemy.orm import relationship

from padel_app.sql_db import db
from padel_app import model
from padel_app.tools.input_tools import Block, Field, Form


class PlayerInvitation(db.Model, model.Model):
    __tablename__ = "player_invitations"
    __table_args__ = {"extend_existing": True}

    page_title = "Player Invitations"
    model_name = "PlayerInvitation"

    id = Column(Integer, primary_key=True)

    player_id = Column(
        Integer, ForeignKey("players.id", ondelete="CASCADE"), nullable=False
    )
    player = relationship("Player")

    token = Column(String(64), unique=True, nullable=False, index=True)

    invited_by_coach_id = Column(Integer, ForeignKey("coaches.id"))
    invited_by_coach = relationship("Coach")

    status = Column(
        Enum(
            "pending",
            "accepted",
            "revoked",
            "expired",
            name="player_invitation_status",
        ),
        nullable=False,
        server_default="pending",
    )
    expires_at = Column(DateTime, nullable=False)

    @property
    def name(self):
        return f"Invitation for player {self.player_id} ({self.status})"

    def __repr__(self):
        return f"<PlayerInvitation player={self.player_id} status={self.status}>"

    def __str__(self):
        return self.name

    @property
    def display_name(self):
        return str(self)

    @classmethod
    def display_all_info(cls):
        searchable = {"field": "token", "label": "Token"}
        columns = [
            {"field": "player", "label": "Player"},
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
                get_field(
                    "player", "ManyToOne", label="Player", related_model="Player"
                ),
                get_field("token", "Text", label="Token"),
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
