from datetime import datetime, timedelta, time

from padel_app.sql_db import db


# ---------------------------------------------------------------------------
# Overlap / validation
# ---------------------------------------------------------------------------

def _seasons_overlap(start_a, end_a, start_b, end_b) -> bool:
    """Inclusive overlap check between two [start, end] date ranges."""
    return start_a <= end_b and start_b <= end_a


def _parse_date(value):
    if value is None:
        raise ValueError("Season start and end dates are required")
    if hasattr(value, "isoformat") and not isinstance(value, str):
        return value
    return datetime.strptime(value, "%Y-%m-%d").date()


def validate_no_overlap(coach, start_date, end_date, exclude_id=None):
    """Raise ValueError if the given range is invalid or overlaps an existing
    season of the coach (other than the one identified by exclude_id)."""
    if start_date > end_date:
        raise ValueError("Season start must be before end")

    for season in coach.seasons:
        if exclude_id is not None and season.id == exclude_id:
            continue
        if _seasons_overlap(start_date, end_date, season.start_date, season.end_date):
            raise ValueError("Overlapping seasons are not allowed")


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

def list_seasons(coach):
    return sorted(coach.seasons, key=lambda s: s.start_date)


def resolve_season_end_for_coach(coach, on_date):
    """Return the end_date of the coach's season covering on_date, else None."""
    for season in coach.seasons:
        if season.start_date <= on_date <= season.end_date:
            return season.end_date
    return None


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------

def upsert_seasons(coach, data):
    """Batch upsert seasons from a list of {id?, name, startDate, endDate}.

    Validates the intended FINAL set for pairwise overlaps before writing.
    Deletes coach seasons not present in the incoming set, then updates/creates
    the rest. Returns the coach's resulting seasons.
    """
    from padel_app.models import Season

    entries = []
    for entry in data:
        raw_id = entry.get("id")
        entry_id = int(raw_id) if raw_id not in (None, "", "null") else None
        start_date = _parse_date(entry.get("startDate"))
        end_date = _parse_date(entry.get("endDate"))
        name = entry.get("name")

        if start_date > end_date:
            raise ValueError("Season start must be before end")

        entries.append(
            {
                "id": entry_id,
                "name": name,
                "start_date": start_date,
                "end_date": end_date,
            }
        )

    # Validate the final set for pairwise overlaps.
    for i in range(len(entries)):
        for j in range(i + 1, len(entries)):
            if _seasons_overlap(
                entries[i]["start_date"],
                entries[i]["end_date"],
                entries[j]["start_date"],
                entries[j]["end_date"],
            ):
                raise ValueError("Overlapping seasons are not allowed")

    incoming_ids = {e["id"] for e in entries if e["id"] is not None}

    # Delete coach seasons not in the incoming set.
    for season in list(coach.seasons):
        if season.id not in incoming_ids:
            db.session.delete(season)
    db.session.flush()

    # Update / create the rest.
    for e in entries:
        season = None
        if e["id"] is not None:
            season = Season.query.filter_by(id=e["id"], coach_id=coach.id).first()

        if season is None:
            season = Season(coach_id=coach.id)
            db.session.add(season)

        season.name = e["name"]
        season.start_date = e["start_date"]
        season.end_date = e["end_date"]

    db.session.commit()
    return list_seasons(coach)


def delete_season(coach, season_id):
    from padel_app.models import Season

    season = Season.query.filter_by(id=int(season_id), coach_id=coach.id).first()
    if season is None:
        return False
    db.session.delete(season)
    db.session.commit()
    return True


# ---------------------------------------------------------------------------
# Instance regeneration (future-only)
# ---------------------------------------------------------------------------

def regenerate_future_instances_for_season(season, *, now=None):
    """Re-cap FUTURE recurring instances governed by this season.

    For each of the coach's recurring lessons that recur-until-season-end and
    whose start date falls within the season window, set the lesson's
    recurrence_end to the season end and prune any LessonInstances strictly
    after the season end. Past/held instances are never touched.
    """
    from padel_app.services.lesson_service import delete_future_instances
    from padel_app.utils.dates import utcnow_naive

    if now is None:
        now = utcnow_naive()

    coach = season.coach
    cutoff = datetime.combine(season.end_date + timedelta(days=1), time.min)

    for lesson in coach.lessons:
        if not getattr(lesson, "recurs_until_season_end", False):
            continue

        lesson_date = lesson.start_datetime.date()
        if not (season.start_date <= lesson_date <= season.end_date):
            continue

        lesson.recurrence_end = season.end_date
        lesson.save()

        # Prune instances strictly after the (new) season end.
        delete_future_instances(lesson, cutoff)

        # Reschedule reminder jobs within the new horizon (no-op in tests).
        try:
            from padel_app.scheduler import (
                schedule_lesson_reminder_jobs,
                cancel_lesson_reminder_jobs,
            )

            if lesson.coaches_relations:
                coach_id = lesson.coaches_relations[0].coach_id
                cancel_lesson_reminder_jobs(
                    lesson.id, from_date=season.end_date + timedelta(days=1)
                )
                schedule_lesson_reminder_jobs(lesson.id, coach_id)
        except Exception:
            pass
