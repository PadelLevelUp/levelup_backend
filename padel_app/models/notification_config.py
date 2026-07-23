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


# App-wide fallback locale — the last link in every locale chain.
DEFAULT_LOCALE = "pt"

SUPPORTED_TEMPLATE_LOCALES = ("pt", "en")


def normalize_locale(locale) -> "str | None":
    """Map any language tag to a supported locale, or ``None`` when unset.

    Accepts whatever the callers have on hand: ``User.language`` ("pt"/"en", the
    column ``PATCH /api/auth/me`` writes), a fuller tag like "pt-PT", or junk.
    Returning ``None`` (rather than a default) for missing/blank input is what
    lets callers chain fallbacks — recipient → coach → :data:`DEFAULT_LOCALE`.
    """
    if not isinstance(locale, str):
        return None
    tag = locale.strip().lower()
    if not tag:
        return None
    return "pt" if tag.startswith("pt") else "en"


def default_templates_for_locale(locale):
    return (
        DEFAULT_MESSAGE_TEMPLATES_PT
        if (normalize_locale(locale) or DEFAULT_LOCALE) == "pt"
        else DEFAULT_MESSAGE_TEMPLATES
    )


class ResolvedTemplate(str):
    """Template text plus the locale its wording is actually in.

    Subclasses ``str`` so every call site (and ``_format_template``) keeps
    treating it as plain text, while callers that also render locale-aware
    placeholders — ``{weekday}`` is formatted through Babel — can read
    ``.locale`` and stay consistent with the sentence around it. Without this a
    default resolved into English could end up carrying a Portuguese weekday.
    """

    __slots__ = ("locale",)

    def __new__(cls, text: str, locale: str):
        obj = super().__new__(cls, text)
        obj.locale = locale
        return obj


def _is_blank_template(value) -> bool:
    """True when a stored template can't produce a message body.

    Covers the three ways a template ends up unusable: the key was never saved
    (``None``), the coach cleared the textarea (``""`` / whitespace only), or the
    JSON holds a non-string (legacy data, bad payload).
    """
    return not isinstance(value, str) or not value.strip()


def resolve_message_template(
    templates, key, locale=None, *, recipient_locale=None
) -> ResolvedTemplate:
    """Resolve one template key to non-empty text in the right language.

    This is the ONE place that decides both *what* text an automatic message
    carries and *which* language it is in.

    ``templates`` holds the coach's own customisations — normally
    :meth:`NotificationConfig.get_custom_message_templates`. ``locale`` is the
    coach's configured locale (the language their prose is written in);
    ``recipient_locale`` is the language of the person the message is addressed
    to.

    Two rules, deliberately different:

    * **A coach's custom template is their prose.** It is delivered verbatim, in
      whatever language they typed it, to every recipient. It is never
      translated (PAD-67 follow-up: no translation API, no LLM call).
    * **A built-in default is ours**, so it renders in the *recipient's*
      language. An English student of a Portuguese coach gets the English
      default, and vice versa.

    Fallback chain when a default is used: ``recipient_locale`` →  ``locale``
    (the coach's) → :data:`DEFAULT_LOCALE`. A recipient with no language set
    simply contributes ``None`` and the chain moves on.

    Returns a :class:`ResolvedTemplate` — a ``str`` that also reports the locale
    its wording is in, so placeholders like ``{weekday}`` can be formatted to
    match.
    """
    author_locale = normalize_locale(locale)
    value = (templates or {}).get(key)
    if not _is_blank_template(value):
        return ResolvedTemplate(value, author_locale or DEFAULT_LOCALE)
    target = normalize_locale(recipient_locale) or author_locale or DEFAULT_LOCALE
    fallback = default_templates_for_locale(target).get(key)
    if not _is_blank_template(fallback):
        return ResolvedTemplate(fallback, target)
    # Unknown key with no built-in default — the caller must not send anything.
    return ResolvedTemplate("", target)


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

    def get_custom_message_templates(self) -> dict:
        """Only the keys this coach actually customised, with usable text.

        This — not :meth:`get_message_templates` — is what the message engine
        must send with. A key absent here has no coach prose behind it, which
        leaves :func:`resolve_message_template` free to pick the built-in
        default in the *recipient's* language. ``get_message_templates``
        pre-merges the defaults at the *coach's* locale and so erases the
        "customised vs default" distinction; using it for sending is what made
        an English student of a Portuguese coach receive Portuguese defaults.

        Blank/whitespace-only/non-string values are dropped for the same reason
        they are ignored in the merge (PAD-67): they cannot produce a body.
        """
        stored = self.message_templates
        if not isinstance(stored, dict):
            return {}
        return {k: v for k, v in stored.items() if not _is_blank_template(v)}

    def get_message_templates(self, locale=None):
        """Coach templates merged over the locale defaults, never blank (PAD-67).

        A stored key that is missing, ``null``, non-string or blank/whitespace-
        only does NOT override the default — otherwise the engine would render
        and deliver an empty message body. Customisations that carry actual text
        always win, and unknown extra keys are preserved as-is.

        This is the *coach-facing* view (Settings → Message templates, served by
        ``get_config_dict``): the defaults it fills in are shown in the coach's
        own language. Do NOT use it to render an outgoing message — see
        :meth:`get_custom_message_templates`.
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
