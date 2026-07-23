from sqlalchemy import Boolean, Column, ForeignKey, Integer, JSON, String
from sqlalchemy.orm import relationship

from padel_app.sql_db import db
from padel_app import model


DEFAULT_PRIORITY_CRITERIA = [
    {"id": "level", "label": "Level", "enabled": True},
    {"id": "justified_misses", "label": "Justified Misses", "enabled": True},
    {"id": "attendance", "label": "Attendance", "enabled": True},
    {"id": "playing_side", "label": "Playing Side", "enabled": False},
    {"id": "subscription_status", "label": "Subscription Status", "enabled": False},
]

DEFAULT_RESTRICTIONS = {
    "maxSimultaneous": {"enabled": True, "value": 3},
    "maxTotal": {"enabled": True, "value": 10},
    "minTimeBeforeClass": {"enabled": False, "value": 30},
    "maxInvitesPerStudentPerDay": {"enabled": False, "value": 3},
    "quietHours": {"enabled": False},
    "maxInactiveTime": {"enabled": True, "value": 120},
    "excludedPlayers": {"enabled": False, "playerIds": []},
    "excludeUnpaidSubscription": {"enabled": False},
    # Hours before class start after which a student cancellation is flagged
    # as a late cancellation (spot is still freed). Plain scalar (hours).
    "cancellationDeadlineHours": 24,
}

DEFAULT_ROUNDS = [
    {
        "id": 1,
        "criteria": ["same_level", "same_side"],
        "criteria_values": {},
        "description": "Exact match",
    },
    {
        "id": 2,
        "criteria": ["same_level"],
        "criteria_values": {},
        "description": "Same level",
    },
    {
        "id": 3,
        "criteria": [],
        "criteria_values": {},
        "description": "Open to all",
    },
]

DEFAULT_NOTIFICATION_GROUPS = [
    {"id": "same_level", "label": "Same level", "enabled": True},
    {"id": "recent_absences", "label": "Recent absences", "enabled": True},
    {"id": "justified_absences", "label": "Justified absences", "enabled": True},
    {"id": "all_students", "label": "All students", "enabled": True},
]

DEFAULT_MESSAGE_TEMPLATES = {
    "invite": "Hey {name}, we have an opening in the {level} class next {weekday} at {time}. Do you want to come?",
    "confirm": "Great! I'm counting on you! See you there 🎾",
    "decline": "No problem, see you next time!",
    "spot_filled": "Sorry, this place was filled already! I'll get back to you if something else opens up.",
    "reminder": "Hey {name}, just a reminder that you have the {level} class this {weekday} at {time}. Are you coming?",
    "reminder_followup": "Hey {name}, still haven't heard back — do you have a spot for the {level} class this {weekday} at {time}?",
    "reminder_confirmed": "Great, see you then! 🎾",
    "reminder_declined": "Got it, thanks for letting us know!",
    "waiting_list_offer": "This spot was just taken, but we can put you on the waiting list and notify you if another opens up. Interested?",
    "waiting_list_confirm": "You're on the waiting list! We'll let you know if a spot opens.",
    "waiting_list_placed": "Good news {name}! A spot opened up in the {level} class on {weekday} at {time} and you've been added. See you there! 🎾",
}

DEFAULT_MESSAGE_TEMPLATES_PT = {
    "invite": "Olá {name}, abriu uma vaga na aula de {level} na próxima {weekday} às {time}. Queres vir?",
    "confirm": "Boa! Conto contigo! Até já 🎾",
    "decline": "Sem problema, para a próxima!",
    "spot_filled": "Desculpa, esta vaga já foi preenchida! Aviso-te se abrir outra.",
    "reminder": "Olá {name}, lembrete: tens a aula de {level} esta {weekday} às {time}. Vens?",
    "reminder_followup": "Olá {name}, ainda não tive resposta — tens vaga para a aula de {level} esta {weekday} às {time}?",
    "reminder_confirmed": "Boa, até já! 🎾",
    "reminder_declined": "Entendido, obrigado por avisares!",
    "waiting_list_offer": "Esta vaga acabou de ser ocupada, mas podemos pôr-te na lista de espera e avisar-te se abrir outra. Interessa?",
    "waiting_list_confirm": "Estás na lista de espera! Avisamos-te se abrir uma vaga.",
    "waiting_list_placed": "Boas notícias {name}! Abriu uma vaga na aula de {level} na {weekday} às {time} e foste adicionado. Até já! 🎾",
}


def default_templates_for_locale(locale):
    return DEFAULT_MESSAGE_TEMPLATES_PT if (locale or "pt").startswith("pt") else DEFAULT_MESSAGE_TEMPLATES


def _is_blank_template(value) -> bool:
    """True when a stored template can't produce a message body.

    Covers the three ways a template ends up unusable: the key was never saved
    (``None``), the coach cleared the textarea (``""`` / whitespace only), or the
    JSON holds a non-string (legacy data, bad payload).
    """
    return not isinstance(value, str) or not value.strip()


def resolve_message_template(templates, key, locale=None) -> str:
    """Resolve one template key to non-empty text (PAD-67).

    ``templates`` is whatever the caller has on hand — normally the dict from
    :meth:`NotificationConfig.get_message_templates`, but call sites also pass a
    raw defaults dict. Whenever the stored value is missing or blank/whitespace-
    only, fall back to the built-in default for ``locale`` so an automatic
    message is never delivered empty.
    """
    value = (templates or {}).get(key)
    if not _is_blank_template(value):
        return value
    fallback = default_templates_for_locale(locale).get(key)
    if not _is_blank_template(fallback):
        return fallback
    # Unknown key with no built-in default — the caller must not send anything.
    return ""


DEFAULT_INVITATION_GROUPS = [
    {
        "id": "1",
        "rules": [
            {"attribute": "level", "operation": "same_as_vacancy"},
            {"attribute": "side", "operation": "same_as_vacancy"},
        ],
    },
    {
        "id": "2",
        "rules": [{"attribute": "level", "operation": "same_as_vacancy"}],
    },
    {"id": "3", "rules": []},
]

DEFAULT_TIEBREAKERS = [
    {"id": "unjustified_absences", "label": "Fewest unjustified absences", "enabled": True},
    {"id": "justified_absences", "label": "Most justified absences", "enabled": True},
    {"id": "attendance_rate", "label": "Highest attendance rate", "enabled": True},
    {"id": "playing_side_match", "label": "Matching playing side", "enabled": False},
    {"id": "subscription_status", "label": "Active subscription", "enabled": False},
]

# Hours before a class start after which a student cancellation is flagged as a
# late cancellation. Students may still cancel (and the spot is still freed), but
# the resulting Presence is marked late_cancellation=True. Stored in the
# ``restrictions`` JSON under ``cancellationDeadlineHours``. Default 24h.
DEFAULT_CANCELLATION_DEADLINE_HOURS = 24

DEFAULT_REMINDER_TIMING = {"type": "hours_before", "value": 48}
DEFAULT_INVITATION_START_TIMING = {"type": "hours_before", "value": 24}
# How many reminders to send each student, and how far apart, when they
# don't respond. Stored alongside ``firstReminder`` in the reminder_timing JSON.
DEFAULT_REMINDER_COUNT = 1
DEFAULT_HOURS_BETWEEN_REMINDERS = 24


class NotificationConfig(db.Model, model.Model):
    __tablename__ = "notification_configs"
    __table_args__ = {"extend_existing": True}

    page_title = "Notification Config"
    model_name = "NotificationConfig"

    id = Column(Integer, primary_key=True)
    coach_id = Column(
        Integer, ForeignKey("coaches.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    auto_notify_enabled = Column(Boolean, default=False, nullable=False)
    # "automatic" (default) or "semi_automatic" — only relevant when
    # auto_notify_enabled is true. In semi_automatic mode vacancies require
    # coach approval before invitations are sent.
    invitation_mode = Column(
        String(20), default="automatic", server_default="automatic", nullable=False
    )
    priority_criteria = Column(JSON, nullable=True)
    restrictions = Column(JSON, nullable=True)
    rounds = Column(JSON, nullable=True)
    notification_groups = Column(JSON, nullable=True)
    message_templates = Column(JSON, nullable=True)
    reminder_timing = Column(JSON, nullable=True)
    invitation_start_timing = Column(JSON, nullable=True)
    invitation_groups = Column(JSON, nullable=True)
    tiebreakers = Column(JSON, nullable=True)

    coach = relationship("Coach")

    @property
    def name(self):
        return f"NotificationConfig for coach {self.coach_id}"

    def get_invitation_mode(self):
        return self.invitation_mode or "automatic"

    def get_priority_criteria(self):
        return self.priority_criteria if self.priority_criteria is not None else DEFAULT_PRIORITY_CRITERIA

    def get_restrictions(self):
        if self.restrictions is None:
            return DEFAULT_RESTRICTIONS
        # Merge stored restrictions with defaults so new keys are always present
        return {**DEFAULT_RESTRICTIONS, **self.restrictions}

    def get_cancellation_deadline_hours(self):
        """Hours before class start after which a cancellation is flagged late.

        Read from the ``cancellationDeadlineHours`` key of the restrictions JSON,
        defaulting to ``DEFAULT_CANCELLATION_DEADLINE_HOURS`` (24) when unset or
        invalid.
        """
        raw = self.get_restrictions().get(
            "cancellationDeadlineHours", DEFAULT_CANCELLATION_DEADLINE_HOURS
        )
        try:
            hours = float(raw)
        except (TypeError, ValueError):
            return DEFAULT_CANCELLATION_DEADLINE_HOURS
        if hours < 0:
            return DEFAULT_CANCELLATION_DEADLINE_HOURS
        return hours

    def get_rounds(self):
        return self.rounds if self.rounds is not None else DEFAULT_ROUNDS

    def get_notification_groups(self):
        return self.notification_groups if self.notification_groups is not None else DEFAULT_NOTIFICATION_GROUPS

    def get_message_templates(self, locale=None):
        """Coach templates merged over the locale defaults, never blank (PAD-67).

        A stored key that is missing, ``null``, non-string or blank/whitespace-
        only does NOT override the default — otherwise the engine would render
        and deliver an empty message body. Customisations that carry actual text
        always win, and unknown extra keys are preserved as-is.
        """
        defaults = default_templates_for_locale(locale)
        stored = self.message_templates
        if not isinstance(stored, dict):
            return dict(defaults)
        merged = {**defaults, **stored}
        for key, default_text in defaults.items():
            if _is_blank_template(merged.get(key)):
                merged[key] = default_text
        return merged

    def get_reminder_timing(self):
        if self.reminder_timing is None:
            return DEFAULT_REMINDER_TIMING
        # UI stores a nested ReminderConfig; extract the flat timing sub-object
        if "firstReminder" in self.reminder_timing:
            return self.reminder_timing["firstReminder"]
        return self.reminder_timing  # already flat (legacy / default)

    def get_reminder_count(self):
        """Number of reminders to send each student (floor 1).

        Read from the ``reminderCount`` key of the reminder_timing JSON, with the
        same defensive shape handling as ``get_reminder_timing``.
        """
        rt = self.reminder_timing
        if not isinstance(rt, dict):
            return DEFAULT_REMINDER_COUNT
        raw = rt.get("reminderCount", DEFAULT_REMINDER_COUNT)
        try:
            count = int(raw)
        except (TypeError, ValueError):
            return DEFAULT_REMINDER_COUNT
        return max(1, count)

    def get_hours_between_reminders(self):
        """Hours to wait between consecutive reminders (must be > 0).

        Read from the ``hoursBetweenReminders`` key of the reminder_timing JSON.
        """
        rt = self.reminder_timing
        if not isinstance(rt, dict):
            return DEFAULT_HOURS_BETWEEN_REMINDERS
        raw = rt.get("hoursBetweenReminders", DEFAULT_HOURS_BETWEEN_REMINDERS)
        try:
            hours = float(raw)
        except (TypeError, ValueError):
            return DEFAULT_HOURS_BETWEEN_REMINDERS
        if hours <= 0:
            return DEFAULT_HOURS_BETWEEN_REMINDERS
        return hours

    def get_invitation_start_timing(self):
        # Prefer invitationStart embedded in reminderTiming (set by UI)
        if self.reminder_timing and "invitationStart" in self.reminder_timing:
            return self.reminder_timing["invitationStart"]
        if self.invitation_start_timing is not None:
            return self.invitation_start_timing
        return DEFAULT_INVITATION_START_TIMING

    def get_invitation_groups(self):
        return self.invitation_groups if self.invitation_groups is not None else DEFAULT_INVITATION_GROUPS

    def get_tiebreakers(self):
        return self.tiebreakers if self.tiebreakers is not None else DEFAULT_TIEBREAKERS

    @classmethod
    def get_create_form(cls):
        from padel_app.tools.input_tools import Block, Field, Form
        form = Form()
        form.add_block(Block("info_block", fields=[
            Field(instance_id=cls.id, model=cls.model_name, name="coach", label="Coach", type="ManyToOne", related_model="Coach"),
        ]))
        return form
