"""Blocks for the rebuilt coach dashboard.

The screen answers one question — *what needs me right now?* — so the payload is
four blocks in priority order: the class about to start, a queue of things that
can be resolved, the week ahead, and two health metrics.

Every number here ships with its denominator. A bare count (52 players, 19
classes) tells a coach nothing about whether anything is wrong, which is why the
old ``kpi_grid`` is gone: ``pending_validations`` became a queue item that can be
cleared, and the two remaining counts moved into ``week_pulse`` with context.

Notes on two deliberate choices:

* **No court.** ``Lesson`` has no court/field column, so the hero shows
  ``{start} – {end}`` only rather than inventing a location.
* **Validation is a last-7-days window.** The old KPI counted every unvalidated
  presence ever recorded. Scoped to classes that ended in the last week, the
  number matches the "From {n} classes last week" framing and can actually reach
  zero; attendances older than that are a backlog, not this week's chore.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlencode

from sqlalchemy import func, or_

from padel_app.sql_db import db
from padel_app.models import (
    Association_CoachLesson,
    Association_CoachPlayer,
    Association_PlayerLessonInstance,
    ConversationParticipant,
    Lesson,
    LessonInstance,
    Message,
    Player,
    Presence,
    User,
)
from padel_app.helpers.calendar_helpers import (
    build_lesson_events,
    load_lessons_for_coach,
    load_lesson_instances_for_coach,
)
from padel_app.tools.tools import _safe_int
from padel_app.utils.dates import utcnow_naive

# How far ahead the hero and the queue look.
HERO_SOON_MINUTES = 120
SCHEDULE_DAYS = 7
SCHEDULE_ROWS = 5
QUEUE_REPLY_LIMIT = 3
HERO_AVATAR_LIMIT = 3
ACTIVE_PLAYER_DAYS = 30
VALIDATION_WINDOW_DAYS = 7

_EPOCH = datetime(1970, 1, 1)


# ── shared event loading ───────────────────────────────────────────────────


def _load_events(*, coach_id: int, start: datetime, end: datetime) -> List[Dict[str, Any]]:
    """Coach classes overlapping a window, earliest first.

    Deliberately ignores the serialized ``status``: that field is computed
    against ``utcnow_naive()`` inside the serializer, so it would silently
    override an injected ``now`` and make every time-dependent test here a lie.
    The window bounds are the only thing deciding what is in range.
    """
    lessons = load_lessons_for_coach(coach_id, start, end)
    instances = load_lesson_instances_for_coach(coach_id, start, end)
    events = build_lesson_events(lessons, instances, start, end)

    in_window = [
        e
        for e in events
        if e.get("type") == "class" and _event_end(e) > start and _event_start(e) < end
    ]
    return sorted(in_window, key=_event_start)


def _event_start(event: Dict[str, Any]) -> datetime:
    return _combine(event.get("date"), event.get("startTime"), datetime.max)


def _event_end(event: Dict[str, Any]) -> datetime:
    return _combine(event.get("date"), event.get("endTime"), datetime.min)


def _combine(day: Optional[str], clock: Optional[str], fallback: datetime) -> datetime:
    try:
        return datetime.fromisoformat(f"{day}T{clock}")
    except (TypeError, ValueError):
        return fallback


def _class_href(event: Dict[str, Any]) -> str:
    """Deep link that opens this one occurrence in the calendar.

    Both params are required — a materialized id is just ``lessoninstance-<pk>``,
    so the date cannot be derived from it, and the calendar needs the date to
    select the right week.
    """
    return "/calendar?" + urlencode(
        {"classId": str(event.get("id") or ""), "date": str(event.get("date") or "")}
    )


def _fill(event: Dict[str, Any]) -> Tuple[int, int]:
    return _safe_int(event.get("participantCount"), 0), _safe_int(event.get("maxPlayers"), 0)


def _initials(name: Optional[str]) -> str:
    parts = [p for p in (name or "").split() if p]
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[-1][0]).upper()


# ── 1. next class hero ─────────────────────────────────────────────────────


def build_next_class_block(*, coach_id: int, now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
    """The class about to start. ``None`` when the coach has nothing scheduled.

    Returning ``None`` is intentional: an empty hero would be the largest element
    on the screen saying nothing, which is the flaw this redesign removes.
    """
    now = now or utcnow_naive()
    events = _load_events(coach_id=coach_id, start=now, end=now + timedelta(days=90))
    if not events:
        return None

    event = events[0]
    start = _event_start(event)
    filled, capacity = _fill(event)
    is_today = start.date() == now.date()
    minutes_until = int((start - now).total_seconds() // 60)

    return {
        "id": "next_class",
        "type": "next_class",
        "data": {
            "classId": str(event.get("id") or ""),
            "title": event.get("title") or "",
            "date": event.get("date"),
            "startTime": event.get("startTime"),
            "endTime": event.get("endTime"),
            "isToday": is_today,
            # Drives "UP NEXT · 18:00" vs "NEXT CLASS · Tuesday". The client
            # localises the weekday from `date`; this is just the switch.
            "weekday": start.strftime("%A"),
            # Only set when the chip should show, so the client never has to
            # re-derive the 2-hour rule.
            "minutesUntil": minutes_until if is_today and 0 <= minutes_until <= HERO_SOON_MINUTES else None,
            "filled": filled,
            "capacity": capacity,
            "players": _roster(event, limit=HERO_AVATAR_LIMIT),
            "href": _class_href(event),
        },
    }


def _roster(event: Dict[str, Any], *, limit: int) -> List[Dict[str, Any]]:
    """Signed-up players for the avatar stack, capped at ``limit``.

    An unmaterialized occurrence has no instance row, so enrolment comes off the
    lesson template instead.
    """
    model = event.get("model")
    original_id = event.get("originalId")
    if not original_id:
        return []

    if model == "LessonInstance":
        rows = (
            db.session.query(Player)
            .join(
                Association_PlayerLessonInstance,
                Association_PlayerLessonInstance.player_id == Player.id,
            )
            .filter(Association_PlayerLessonInstance.lesson_instance_id == original_id)
            .all()
        )
    else:
        lesson = db.session.get(Lesson, original_id)
        rows = [rel.player for rel in getattr(lesson, "players_relations", [])] if lesson else []

    return [
        {"id": p.id, "name": p.name, "initials": _initials(p.name)}
        for p in rows[:limit]
        if p is not None
    ]


# ── 2. needs-you queue ─────────────────────────────────────────────────────


def build_needs_you_block(*, coach_id: int, user_id: int, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Things the coach can resolve, each carrying its own action.

    Order is fixed — empty seats (soonest first), then replies, then validation —
    because it runs from time-critical to whenever-you-like.
    """
    now = now or utcnow_naive()

    items: List[Dict[str, Any]] = []
    items.extend(_empty_seat_items(coach_id=coach_id, now=now))
    items.extend(_reply_items(user_id=user_id))

    validation = _validation_item(coach_id=coach_id, now=now)
    if validation:
        items.append(validation)

    return {
        "id": "needs_you",
        "type": "needs_you",
        "data": {"count": len(items), "items": items},
    }


def _empty_seat_items(*, coach_id: int, now: datetime) -> List[Dict[str, Any]]:
    events = _load_events(coach_id=coach_id, start=now, end=now + timedelta(days=SCHEDULE_DAYS))
    out: List[Dict[str, Any]] = []
    for event in events:
        filled, capacity = _fill(event)
        if not capacity or filled >= capacity:
            continue
        out.append(
            {
                "kind": "empty_seats",
                "id": str(event.get("id") or ""),
                "classTitle": event.get("title") or "",
                "seatsMissing": capacity - filled,
                "date": event.get("date"),
                "timeLabel": event.get("startTime"),
                "filled": filled,
                "capacity": capacity,
                "href": _class_href(event),
            }
        )
    return out


def _reply_items(*, user_id: int) -> List[Dict[str, Any]]:
    """Unread inbound messages, most recent first, one per conversation."""
    rows = (
        db.session.query(Message, User, ConversationParticipant.conversation_id)
        .join(ConversationParticipant, ConversationParticipant.conversation_id == Message.conversation_id)
        .join(User, User.id == Message.sender_id)
        .filter(ConversationParticipant.user_id == user_id)
        .filter(Message.sender_id != user_id)
        .filter(Message.is_deleted.is_(False))
        .filter(Message.sent_at > func.coalesce(ConversationParticipant.last_read_at, _EPOCH))
        .order_by(Message.sent_at.desc())
        .all()
    )

    seen: set = set()
    out: List[Dict[str, Any]] = []
    for message, sender, conversation_id in rows:
        if conversation_id in seen:
            continue
        seen.add(conversation_id)
        out.append(
            {
                "kind": "reply",
                "id": f"conversation-{conversation_id}",
                "personName": sender.name,
                "initials": _initials(sender.name),
                "preview": (message.text or "").strip(),
                "href": f"/messages?conversationId={conversation_id}",
            }
        )
        if len(out) >= QUEUE_REPLY_LIMIT:
            break
    return out


def _validation_item(*, coach_id: int, now: datetime) -> Optional[Dict[str, Any]]:
    """Unvalidated attendances for classes that ended in the last week."""
    window_start = now - timedelta(days=VALIDATION_WINDOW_DAYS)

    base = (
        db.session.query(Presence.id, LessonInstance.id.label("instance_id"))
        .join(LessonInstance, Presence.lesson_instance_id == LessonInstance.id)
        .join(Lesson, LessonInstance.lesson_id == Lesson.id)
        .join(Association_CoachLesson, Association_CoachLesson.lesson_id == Lesson.id)
        .filter(Association_CoachLesson.coach_id == coach_id)
        .filter(Presence.validated.is_(False))
        .filter(LessonInstance.end_datetime <= now)
        .filter(LessonInstance.end_datetime >= window_start)
        .subquery()
    )

    count = db.session.query(func.count(base.c.id)).scalar() or 0
    if not count:
        return None

    class_count = db.session.query(func.count(func.distinct(base.c.instance_id))).scalar() or 0

    return {
        "kind": "validation",
        "id": "validation",
        "count": int(count),
        "classCount": int(class_count),
        "href": "/validations",
    }


# ── 3. next 7 days ─────────────────────────────────────────────────────────


def build_schedule_block(*, coach_id: int, now: Optional[datetime] = None) -> Dict[str, Any]:
    """The week ahead. Shows the first few rows and links out for the rest."""
    now = now or utcnow_naive()
    events = _load_events(coach_id=coach_id, start=now, end=now + timedelta(days=SCHEDULE_DAYS))

    items = []
    for event in events[:SCHEDULE_ROWS]:
        start = _event_start(event)
        filled, capacity = _fill(event)
        items.append(
            {
                "id": str(event.get("id") or ""),
                "title": event.get("title") or "",
                "date": event.get("date"),
                # Client localises both; these keep the column widths stable.
                "weekday": start.strftime("%A"),
                "dayOfMonth": start.day,
                "timeLabel": event.get("startTime"),
                "filled": filled,
                "capacity": capacity,
                "href": _class_href(event),
            }
        )

    return {
        "id": "schedule_7d",
        "type": "schedule_7d",
        "data": {
            "totalCount": len(events),
            "items": items,
            "calendarHref": "/calendar",
        },
    }


# ── 4. this week ───────────────────────────────────────────────────────────


def build_week_pulse_block(*, coach_id: int, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Two metrics, each with a denominator, plus a 7-day seats trend."""
    now = now or utcnow_naive()

    week_start = datetime.combine(now.date() - timedelta(days=now.weekday()), datetime.min.time())
    week_end = week_start + timedelta(days=7)

    filled, total = _seats_in_window(coach_id=coach_id, start=week_start, end=week_end)

    prev_filled, prev_total = _seats_in_window(
        coach_id=coach_id, start=week_start - timedelta(days=7), end=week_start
    )
    pct = _pct(filled, total)
    prev_pct = _pct(prev_filled, prev_total)

    trend = []
    for offset in range(SCHEDULE_DAYS - 1, -1, -1):
        day = datetime.combine(now.date() - timedelta(days=offset), datetime.min.time())
        day_filled, day_total = _seats_in_window(coach_id=coach_id, start=day, end=day + timedelta(days=1))
        trend.append(_pct(day_filled, day_total))

    active, total_players = _player_activity(coach_id=coach_id, now=now)

    return {
        "id": "week_pulse",
        "type": "week_pulse",
        "data": {
            "seatsFilled": {
                "pct": pct,
                "filled": filled,
                "total": total,
                # None rather than 0 when there is no prior week to compare, so
                # the client can omit the delta instead of claiming "+0%".
                "deltaPct": (pct - prev_pct) if prev_total else None,
                "trend": trend,
            },
            "players": {
                "active": active,
                "total": total_players,
                "idle": max(total_players - active, 0),
            },
        },
    }


def _pct(part: int, whole: int) -> int:
    return round(100 * part / whole) if whole else 0


def _seats_in_window(*, coach_id: int, start: datetime, end: datetime) -> Tuple[int, int]:
    events = _load_events(coach_id=coach_id, start=start, end=end)
    filled = sum(_fill(e)[0] for e in events)
    total = sum(_fill(e)[1] for e in events)
    return filled, total


def _player_activity(*, coach_id: int, now: datetime) -> Tuple[int, int]:
    """Active = attended recently OR signed up to something upcoming.

    Both halves matter: attendance alone marks a player idle the moment they book
    ahead but haven't played yet, and signups alone ignore regulars between terms.
    """
    player_ids = [
        pid
        for (pid,) in db.session.query(Association_CoachPlayer.player_id)
        .filter(Association_CoachPlayer.coach_id == coach_id)
        .all()
    ]
    if not player_ids:
        return 0, 0

    since = now - timedelta(days=ACTIVE_PLAYER_DAYS)

    attended = {
        pid
        for (pid,) in db.session.query(Presence.player_id)
        .join(LessonInstance, Presence.lesson_instance_id == LessonInstance.id)
        .filter(Presence.player_id.in_(player_ids))
        .filter(Presence.status == "present")
        .filter(LessonInstance.end_datetime >= since)
        .filter(LessonInstance.end_datetime <= now)
        .distinct()
        .all()
    }

    upcoming = {
        pid
        for (pid,) in db.session.query(Association_PlayerLessonInstance.player_id)
        .join(
            LessonInstance,
            LessonInstance.id == Association_PlayerLessonInstance.lesson_instance_id,
        )
        .filter(Association_PlayerLessonInstance.player_id.in_(player_ids))
        .filter(LessonInstance.start_datetime >= now)
        .distinct()
        .all()
    }

    return len(attended | upcoming), len(player_ids)
