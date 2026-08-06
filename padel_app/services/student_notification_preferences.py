"""
PAD-112 — the student's standing "don't invite me" preferences.

A student can silence class-slot solicitations outright, at three independent
levels, from their own Settings:

* ``notif_block_auto_invitations`` — the suggestion/auto-invite engine stops
  considering them eligible;
* ``notif_block_manual_invitations`` — the coach cannot manually invite them to
  an open spot;
* ``notif_block_all`` — every class-slot solicitation is suppressed, attendance
  reminders included.

**Not the same thing as an availability blocker.** ``student_availability_service``
(PAD-28/PAD-107) answers "is this student free during THAT class window?" and is
evaluated against the lesson instance's start/end. This module answers "does this
student want to be asked at all?" and is evaluated against nothing but the user
row. The two compose additively at every enforcement point: a solicitation is
suppressed if EITHER says so.

**Opposite privacy postures, deliberately.** A blocker's title, description and
hours are the student's private calendar and are never exposed to a coach. The
reason here is written by the student precisely so their coach can read it — the
coach otherwise has no way to tell "deliberately silent" from "ignoring me". Do
not route one through the other's helpers.

Enforcement lives in ``notification_service``:

============================  =========================================================
level                         enforced in
============================  =========================================================
auto                          ``get_eligible_students`` / ``_get_eligible_students_for_group``
manual                        ``send_manual_notifications`` (before the NotificationEvent)
all                           ``send_class_reminders`` + the ``_send_system_message`` backstop
============================  =========================================================

The earlier filters are the real enforcement; the choke-point backstop is a
safety net. Blocking only at delivery would leave ``NotificationEvent`` rows
marked "sent" with no message behind them — a vacancy waiting forever on a reply
that can never come.
"""


def get_user_block_preferences(user_id):
    """
    ``{"auto": bool, "manual": bool, "all": bool, "reason": str}`` for a user id.

    Returns the all-false default for an unknown or ``None`` user id, so callers
    never have to special-case a player with no user account (such a player has
    no way to express a preference and therefore never blocks anything).
    """
    from padel_app.models import User

    default = {"auto": False, "manual": False, "all": False, "reason": ""}
    if not user_id:
        return default

    user = User.query.get(user_id)
    if user is None:
        return default

    return {
        "auto": bool(user.notif_block_auto_invitations),
        "manual": bool(user.notif_block_manual_invitations),
        "all": bool(user.notif_block_all),
        "reason": (user.notif_block_reason or "").strip(),
    }


def notification_block_payload(user):
    """
    The coach-facing block fields for a student's ``User`` row (PAD-112).

    Defined here, once, because TWO places must return the identical dict:
    ``player_service._serialize_coach_player_relation`` (the players list and
    the detail page) and ``Player.coach_player_info`` (what ``add_player`` and
    ``edit_player`` echo back). Miss the second and the coach's "notifications
    cut" signal vanishes the instant they edit the student.

    ``notificationsBlocked`` is derived rather than stored so it can never
    disagree with the three flags.
    """
    if user is None:
        return {
            "notificationsBlocked": False,
            "blockAutoInvitations": False,
            "blockManualInvitations": False,
            "blockAllNotifications": False,
            "notificationBlockReason": "",
        }

    auto = bool(user.notif_block_auto_invitations)
    manual = bool(user.notif_block_manual_invitations)
    every = bool(user.notif_block_all)
    return {
        "notificationsBlocked": auto or manual or every,
        "blockAutoInvitations": auto,
        "blockManualInvitations": manual,
        "blockAllNotifications": every,
        "notificationBlockReason": (user.notif_block_reason or "").strip(),
    }


def user_blocks_auto_invitations(user_id):
    """True when this user opted out of AUTOMATIC class invitations.

    ``notif_block_all`` implies it — blocking everything necessarily blocks the
    engine's invitations too.
    """
    prefs = get_user_block_preferences(user_id)
    return prefs["auto"] or prefs["all"]


def user_blocks_manual_invitations(user_id):
    """True when this user opted out of MANUAL (coach-picked) class invitations."""
    prefs = get_user_block_preferences(user_id)
    return prefs["manual"] or prefs["all"]


def user_blocks_all_notifications(user_id):
    """True when this user opted out of every class-slot solicitation."""
    return get_user_block_preferences(user_id)["all"]


def _user_id_for_player(player_id):
    from padel_app.models import Player

    player = Player.query.get(player_id)
    return player.user_id if player else None


def player_blocks_auto_invitations(player_id):
    return user_blocks_auto_invitations(_user_id_for_player(player_id))


def player_blocks_manual_invitations(player_id):
    return user_blocks_manual_invitations(_user_id_for_player(player_id))


def player_blocks_all_notifications(player_id):
    return user_blocks_all_notifications(_user_id_for_player(player_id))


def filter_preference_blocked_coach_players(coach_players):
    """
    Drop the ``Association_CoachPlayer`` rows whose student blocked AUTOMATIC
    invitations (PAD-112).

    Mirrors ``student_availability_service.filter_blocked_coach_players`` so the
    two filters sit side by side in the eligibility engine and read as one idea:
    "candidates who do not want to be asked are not candidates".
    """
    if not coach_players:
        return coach_players
    return [
        cp for cp in coach_players
        if not player_blocks_auto_invitations(cp.player_id)
    ]


def _blocked_entry(player_id):
    """``{"playerId", "name", "reason", "cause"}`` for a preference-blocked player.

    The shape matches ``student_availability_service.blocked_players_for_instance``
    (PAD-107) so both kinds of block land in the SAME ``blocked`` array on the
    notify routes; ``reason`` is the extra key, absent for an availability block.

    ``cause`` is what lets the client tell the two apart inside that shared
    array: ``"preference"`` here, ``"unavailable"`` for PAD-107. The coach is
    told a different thing in each case — "they marked themselves unavailable at
    this hour" versus "they turned invitations off, and here is why" — so the
    distinction has to survive the merge into one list. Do NOT infer it from
    ``reason`` being empty: a student can block notifications without giving one.
    """
    from padel_app.models import Player

    player = Player.query.get(player_id)
    user = player.user if player else None
    return {
        "playerId": int(player_id),
        "name": user.name if user else "",
        "reason": (user.notif_block_reason or "").strip() if user else "",
        "cause": "preference",
    }


def preference_blocked_players(player_ids, *, kind):
    """
    ``[{"playerId", "name", "reason"}]`` for the players in ``player_ids`` who
    blocked solicitations of ``kind``.

    ``kind`` is ``"manual"`` (coach-picked invitations), ``"auto"`` (engine
    invitations) or ``"all"`` (everything, e.g. attendance reminders).
    """
    predicate = {
        "auto": player_blocks_auto_invitations,
        "manual": player_blocks_manual_invitations,
        "all": player_blocks_all_notifications,
    }[kind]

    return [
        _blocked_entry(player_id)
        for player_id in (player_ids or [])
        if predicate(player_id)
    ]
