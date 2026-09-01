"""Coach-facing presence overview — roster stats, trend and the validation queue.

PAD-140 ("Presences" tab). Three read surfaces, all coach-scoped:

* :func:`build_presence_stats` — the per-player metric table (total, private vs
  academy, justified vs unjustified absences, guest attendances).
* :func:`build_presence_trend` — one roster-wide time series, bucketed with the
  same day/month/year rules the per-player history page already uses.
* :func:`list_pending_validation` — the classes that have already run but whose
  attendance the coach has not finalized yet, with each player's current state.

Three facts shape everything here:

* ``presences`` has no date column of its own. Every timestamp comes from the
  joined ``lesson_instances.start_datetime``, which is stored naive-UTC — same
  convention as ``attendance_history_service``.
* A coach owns a class through ``Association_CoachLesson`` on the *parent
  lesson*. That join is what scopes every query below to one coach's world.
* "Attended" is ``Presence.status == "present"`` — the same predicate as
  ``compute_player_kpis().lessons_attended`` and ``build_attendance_history``,
  so this tab can never disagree with the dashboard or the history page.

**Guest attendance.** ``Presence.invited`` is NOT a guest signal: materialization
sets it True for every enrolled player (``lesson_service.get_or_materialize_instance``).
A guest is a player who has a presence for an instance but no
``Association_PlayerLesson`` row for its parent lesson — i.e. someone who was
invited into a one-off spot rather than being enrolled in the recurring class.
That LEFT JOIN being NULL is the definition used throughout this module.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import case, func
from sqlalchemy.orm import joinedload

from padel_app.models import (
    Association_CoachLesson,
    Association_CoachPlayer,
    Association_PlayerLesson,
    Lesson,
    LessonInstance,
    Player,
    Presence,
)
from padel_app.services.attendance_history_service import (
    GRANULARITIES,
    _as_naive_utc,
    _bucket_series,
    _bucket_start,
    pick_granularity,
)
from padel_app.sql_db import db


def default_overview_range(now: Optional[datetime] = None) -> Tuple[datetime, datetime]:
    """The trailing 90 days — the default window when the caller pins no range.

    Deliberately wider than the per-player history page's current-month default:
    this tab is a roster-wide overview, and a single month of a small academy can
    be too sparse for the trend chart to say anything.
    """
    end = _as_naive_utc(now or datetime.now(timezone.utc))
    end = end.replace(hour=23, minute=59, second=59, microsecond=0)
    start = (end - timedelta(days=89)).replace(hour=0, minute=0, second=0)
    return start, end


def _normalize_range(range_start: datetime, range_end: datetime) -> Tuple[datetime, datetime]:
    start = _as_naive_utc(range_start)
    end = _as_naive_utc(range_end)
    if end < start:
        start, end = end, start
    return start, end


def _coach_presence_query(coach_id: int, start: datetime, end: datetime):
    """Every presence on a class this coach owns, inside the window.

    The LEFT JOIN onto ``Association_PlayerLesson`` is what lets callers tell an
    enrolled player from a guest: it is NULL exactly when the player is not
    enrolled in the parent lesson.
    """
    return (
        db.session.query(Presence, Lesson, Association_PlayerLesson.id.label("enrolled_id"))
        .join(LessonInstance, Presence.lesson_instance_id == LessonInstance.id)
        .join(Lesson, LessonInstance.lesson_id == Lesson.id)
        .join(Association_CoachLesson, Association_CoachLesson.lesson_id == Lesson.id)
        .outerjoin(
            Association_PlayerLesson,
            (Association_PlayerLesson.lesson_id == Lesson.id)
            & (Association_PlayerLesson.player_id == Presence.player_id),
        )
        .filter(Association_CoachLesson.coach_id == coach_id)
        .filter(LessonInstance.start_datetime >= start)
        .filter(LessonInstance.start_datetime <= end)
    )


def build_presence_stats(
    *,
    coach_id: int,
    range_start: datetime,
    range_end: datetime,
) -> Dict[str, Any]:
    """Per-player presence aggregates across a coach's whole roster.

    Every player on the roster appears, including those with no activity in the
    window — a coach reading "who is drifting away" needs the zero rows most.

    Returns a dict with ``from``/``to``, a ``players`` list (one row per roster
    player) and a ``totals`` block feeding the KPI tiles and the donut chart.
    """
    start, end = _normalize_range(range_start, range_end)

    present = Presence.status == "present"
    absent = Presence.status == "absent"
    is_guest = Association_PlayerLesson.id.is_(None)

    def _count(condition):
        return func.coalesce(func.sum(case((condition, 1), else_=0)), 0)

    rows = (
        _coach_presence_query(coach_id, start, end)
        .with_entities(
            Presence.player_id.label("player_id"),
            _count(present).label("total"),
            _count(present & (Lesson.type == "private")).label("private"),
            _count(present & (Lesson.type == "academy")).label("academy"),
            _count(absent & (Presence.justification == "justified")).label("justified"),
            _count(absent & (Presence.justification == "unjustified")).label("unjustified"),
            _count(is_guest).label("invites_received"),
            _count(is_guest & present).label("invites_joined"),
        )
        .group_by(Presence.player_id)
        .all()
    )
    by_player = {row.player_id: row for row in rows}

    roster = (
        db.session.query(Player)
        .join(
            Association_CoachPlayer,
            Association_CoachPlayer.player_id == Player.id,
        )
        .options(joinedload(Player.user))
        .filter(Association_CoachPlayer.coach_id == coach_id)
        .all()
    )

    players: List[Dict[str, Any]] = []
    for player in roster:
        row = by_player.get(player.id)
        players.append(
            {
                "playerId": player.id,
                "name": player.user.name if player.user else f"Player {player.id}",
                "total": int(row.total) if row else 0,
                "private": int(row.private) if row else 0,
                "academy": int(row.academy) if row else 0,
                "justified": int(row.justified) if row else 0,
                "unjustified": int(row.unjustified) if row else 0,
                "invitesReceived": int(row.invites_received) if row else 0,
                "invitesJoined": int(row.invites_joined) if row else 0,
            }
        )

    players.sort(key=lambda p: (-p["total"], p["name"].lower()))

    total_private = sum(p["private"] for p in players)
    total_academy = sum(p["academy"] for p in players)
    total_presences = sum(p["total"] for p in players)

    return {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "players": players,
        "totals": {
            "presences": total_presences,
            "activePlayers": len(players),
            "private": total_private,
            "academy": total_academy,
            # Guarded so an empty window renders 0%, not a divide-by-zero.
            "academyShare": round(total_academy / total_presences * 100) if total_presences else 0,
            "guestAttendances": sum(p["invitesJoined"] for p in players),
            "justified": sum(p["justified"] for p in players),
            "unjustified": sum(p["unjustified"] for p in players),
        },
    }


def build_presence_trend(
    *,
    coach_id: int,
    range_start: datetime,
    range_end: datetime,
    granularity: Optional[str] = None,
) -> Dict[str, Any]:
    """Roster-wide attended-class counts over time, gap-filled.

    The roster-wide sibling of ``build_attendance_history``: same granularity
    rules, same gap-filling, same naive-UTC handling — reused rather than
    reimplemented so the two charts can never bucket a date differently.
    """
    start, end = _normalize_range(range_start, range_end)
    if granularity not in GRANULARITIES:
        granularity = pick_granularity(start, end)

    rows = (
        _coach_presence_query(coach_id, start, end)
        .with_entities(LessonInstance.start_datetime)
        .filter(Presence.status == "present")
        .all()
    )

    counts: Dict[date, int] = {}
    for (started,) in rows:
        bucket = _bucket_start(started.date(), granularity)
        counts[bucket] = counts.get(bucket, 0) + 1

    buckets = [
        {"start": bucket.isoformat(), "count": counts.get(bucket, 0)}
        for bucket in _bucket_series(start.date(), end.date(), granularity)
    ]

    return {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "granularity": granularity,
        "total": len(rows),
        "buckets": buckets,
    }


def _response_state(presence: Presence) -> str:
    """How the player answered before the class — the RSVP tri-state.

    ``declined`` is a real answer (the student said they weren't coming), so it
    does not block validation; only ``none`` does.

    The ``validated`` guard matters. A coach marking someone absent from the
    class-detail sheet writes exactly the same columns a student decline does
    (``status='absent'``, ``confirmed=False``) — the difference is that
    ``add_presences`` also stamps ``validated=True``. Without the guard this
    would report the coach's own decision back to them as "the student said they
    couldn't make it", which is a claim the student never made. When the record
    is already the coach's, fall back to what ``confirmed`` alone can support.
    """
    if presence.confirmed:
        return "confirmed"
    if presence.validated:
        return "none"
    if presence.status == "absent":
        return "declined"
    return "none"


def _serialize_pending_player(presence: Presence, *, is_guest: bool) -> Dict[str, Any]:
    player = presence.player
    user = player.user if player else None
    return {
        "presenceId": presence.id,
        "playerId": presence.player_id,
        "name": user.name if user else f"Player {presence.player_id}",
        "response": _response_state(presence),
        "status": presence.status,
        "justification": presence.justification,
        "validated": bool(presence.validated),
        "lateCancellation": bool(presence.late_cancellation),
        "guest": is_guest,
    }


def list_pending_validation(
    *,
    coach_id: int,
    range_start: datetime,
    range_end: datetime,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Classes in the window that have already run, split by validation state.

    Only classes whose end has passed are listed: validating a class that has
    not happened yet is meaningless, and ``confirm_presences`` deliberately
    still runs its vacancy/invite side-effects for future instances.

    A class counts as validated when *every* presence on it is validated. There
    is no class-level flag — deliberately: ``calendar.view`` rule 11 guarantees
    a class's ``completed`` status is derived from the clock and never
    coach-settable, so validation is derived from the presence rows instead.
    """
    start, end = _normalize_range(range_start, range_end)
    cutoff = _as_naive_utc(now or datetime.now(timezone.utc))

    instances = (
        db.session.query(LessonInstance)
        .join(Lesson, LessonInstance.lesson_id == Lesson.id)
        .join(Association_CoachLesson, Association_CoachLesson.lesson_id == Lesson.id)
        .options(
            joinedload(LessonInstance.lesson),
            joinedload(LessonInstance.presences)
            .joinedload(Presence.player)
            .joinedload(Player.user),
        )
        .filter(Association_CoachLesson.coach_id == coach_id)
        .filter(LessonInstance.start_datetime >= start)
        .filter(LessonInstance.start_datetime <= end)
        .filter(LessonInstance.end_datetime <= cutoff)
        .filter(LessonInstance.status != "canceled")
        .order_by(LessonInstance.start_datetime.asc())
        .all()
    )

    # One query for every (lesson, player) enrolment touched here, so the guest
    # check below costs nothing per row.
    lesson_ids = {i.lesson_id for i in instances}
    enrolled: set[Tuple[int, int]] = set()
    if lesson_ids:
        enrolled = {
            (row.lesson_id, row.player_id)
            for row in db.session.query(
                Association_PlayerLesson.lesson_id,
                Association_PlayerLesson.player_id,
            )
            .filter(Association_PlayerLesson.lesson_id.in_(lesson_ids))
            .all()
        }

    pending: List[Dict[str, Any]] = []
    validated: List[Dict[str, Any]] = []

    for instance in instances:
        presences = list(instance.presences or [])
        if not presences:
            # A class nobody was enrolled in has nothing to validate.
            continue

        players = [
            _serialize_pending_player(
                presence,
                is_guest=(instance.lesson_id, presence.player_id) not in enrolled,
            )
            for presence in presences
        ]
        players.sort(key=lambda p: (p["response"] != "none", p["name"].lower()))

        unanswered = sum(1 for p in players if p["response"] == "none")
        payload = {
            "lessonInstanceId": instance.id,
            "calendarEventId": f"lessoninstance-{instance.id}",
            "title": instance.title,
            "type": instance.lesson.type if instance.lesson else None,
            "color": getattr(instance.lesson, "color", None),
            "startDatetime": instance.start_datetime.isoformat(),
            "date": instance.start_datetime.date().isoformat(),
            "players": players,
            "unanswered": unanswered,
            # "Ready" mirrors the prototype: everyone answered, so the coach can
            # confirm without deciding anything per-player.
            "ready": unanswered == 0,
        }

        if all(p["validated"] for p in players):
            validated.append(payload)
        else:
            pending.append(payload)

    return {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "pending": pending,
        "validated": validated,
        "pendingCount": len(pending),
    }


def unvalidate_instance(instance: LessonInstance) -> List[Presence]:
    """Reopen a validated class by clearing ``validated`` on its presences.

    The only write in this module. Every other path drives ``validated`` one way
    — False at materialization, True in ``add_presences`` — so Undo needs its
    own door. Status and justification are deliberately left untouched: undo
    reopens the record for editing, it does not erase what the coach recorded.
    """
    presences = list(instance.presences or [])
    for presence in presences:
        presence.validated = False
    db.session.commit()
    return presences
