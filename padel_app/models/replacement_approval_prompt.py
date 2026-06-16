from sqlalchemy import JSON, Column, DateTime, Enum, ForeignKey, Integer, String
from sqlalchemy.orm import relationship

from padel_app import model
from padel_app.sql_db import db


class ReplacementApprovalPrompt(db.Model, model.Model):
    """
    Semi-automatic mode: a coach-facing approval prompt for a single vacancy.

    One prompt per vacancy (vacancy_id is unique). Prompts created by a single
    presence-confirmation call share a bundle_id; a single prompt gets its own
    bundle_id (uniform API — the bundle is always the unit of decision).
    The prompt is persisted as a message in the coach's Assistant conversation.
    """

    __tablename__ = "replacement_approval_prompts"
    __table_args__ = {"extend_existing": True}

    page_title = "Replacement Approval Prompt"
    model_name = "ReplacementApprovalPrompt"

    id = Column(Integer, primary_key=True)

    coach_id = Column(
        Integer, ForeignKey("coaches.id", ondelete="CASCADE"), nullable=False
    )
    vacancy_id = Column(
        Integer,
        ForeignKey("vacancies.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    # uuid4 shared across prompts created by one confirm-presences call
    bundle_id = Column(String(36), nullable=False, index=True)
    # The persisted assistant-conversation message carrying this prompt bundle
    message_id = Column(
        Integer, ForeignKey("messages.id", ondelete="SET NULL"), nullable=True
    )
    declined_player_id = Column(
        Integer, ForeignKey("players.id", ondelete="SET NULL"), nullable=True
    )
    # Full ordered invite queue (all eligible candidates across all
    # rounds/groups, in invite order) at prompt-creation time
    queue_snapshot = Column(JSON, nullable=True)
    # Standing waiting-list match disclosed in the prompt (if any)
    waiting_list_player_id = Column(
        Integer, ForeignKey("players.id", ondelete="SET NULL"), nullable=True
    )

    status = Column(
        Enum(
            "pending",
            "approved",
            "dismissed",
            "stale",
            name="replacement_approval_prompt_status",
        ),
        default="pending",
        server_default="pending",
        nullable=False,
    )
    decided_at = Column(DateTime, nullable=True)

    coach = relationship("Coach")
    vacancy = relationship("Vacancy")
    message = relationship("Message", foreign_keys=[message_id])
    declined_player = relationship("Player", foreign_keys=[declined_player_id])
    waiting_list_player = relationship("Player", foreign_keys=[waiting_list_player_id])

    @property
    def name(self):
        return f"ReplacementApprovalPrompt #{self.id} for vacancy {self.vacancy_id}"

    @classmethod
    def get_create_form(cls):
        from padel_app.tools.input_tools import Block, Field, Form

        form = Form()
        form.add_block(
            Block(
                "info_block",
                fields=[
                    Field(
                        instance_id=cls.id,
                        model=cls.model_name,
                        name="coach",
                        label="Coach",
                        type="ManyToOne",
                        related_model="Coach",
                    ),
                ],
            )
        )
        return form
