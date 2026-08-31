"""PAD-141 — absence history for one player (spec `attendance.absences`).

The decisive property here is NOT "the query returns rows". It is that the two
histories select disjoint sets from the same data, and that the absence total
matches what the dashboard "Missed" KPI reports.

That second one is the whole reason this file is DB-backed rather than another
pure-logic module like `test_attendance_history_buckets.py`: the failure this
guards against is the page and its own entry-point KPI disagreeing, and that can
only be observed by running both predicates over one real fixture.

A page that quietly dropped justified absences would still look correct in
isolation — a chart, some bars, a plausible list — while showing a smaller
number than the card the student clicked to get there. So every test below
seeds BOTH a justified and an unjustified absence.
"""

from datetime import datetime, timedelta

from padel_app.sql_db import db


def _seed_history(app):
    """One player with 2 attended, 2 missed (1 justified, 1 not) classes.

    Returns (player_id, coach_id). All instances sit in a fixed past window so
    the assertions never depend on the wall clock.
    """
    from padel_app.models import User, LessonInstance, Presence
    from padel_app.models.clubs import Club
    from padel_app.models.coaches import Coach
    from padel_app.models.players import Player
    from padel_app.models.lessons import Lesson

    with app.app_context():
        club = Club(name="History Club")
        db.session.add(club)
        db.session.flush()

        coach_user = User(name="Coach", username="hist_coach", password="pw123456")
        player_user = User(name="Pat Player", username="hist_player", password="pw123456")
        db.session.add_all([coach_user, player_user])
        db.session.flush()

        coach = Coach(user_id=coach_user.id)
        player = Player(user_id=player_user.id)
        db.session.add_all([coach, player])
        db.session.flush()

        base = datetime(2026, 3, 10, 10, 0)

        lesson = Lesson(
            title="History Class",
            type="academy",
            club_id=club.id,
            start_datetime=base,
            end_datetime=base + timedelta(hours=1),
            max_players=4,
        )
        db.session.add(lesson)
        db.session.flush()

        # (offset_days, status, justification)
        rows = [
            (0, "present", None),
            (1, "present", None),
            (2, "absent", "justified"),
            (3, "absent", "unjustified"),
        ]
        for offset, status, justification in rows:
            start = base + timedelta(days=offset)
            # `title` is a read-only property derived from the parent lesson.
            instance = LessonInstance(
                lesson_id=lesson.id,
                start_datetime=start,
                end_datetime=start + timedelta(hours=1),
                max_players=4,
            )
            db.session.add(instance)
            db.session.flush()
            db.session.add(
                Presence(
                    player_id=player.id,
                    lesson_instance_id=instance.id,
                    invited=True,
                    confirmed=True,
                    status=status,
                    justification=justification,
                    validated=True,
                )
            )

        db.session.commit()
        return player.id, coach.id


_WINDOW = (datetime(2026, 3, 1), datetime(2026, 3, 31, 23, 59, 59))


class TestAbsenceHistory:
    def test_returns_only_absences(self, app):
        """Rule: the absence page never shows attended classes."""
        from padel_app.services.attendance_history_service import build_absence_history

        player_id, _ = _seed_history(app)
        with app.app_context():
            payload = build_absence_history(
                player_id=player_id,
                range_start=_WINDOW[0],
                range_end=_WINDOW[1],
            )

        assert payload["total"] == 2
        assert len(payload["sessions"]) == 2

    def test_attendance_and_absence_histories_are_disjoint(self, app):
        """The two pages partition the data — no class appears on both.

        Asserted as a set operation rather than two counts: equal counts could
        coincide while both queries returned the same rows.
        """
        from padel_app.services.attendance_history_service import (
            build_absence_history,
            build_attendance_history,
        )

        player_id, _ = _seed_history(app)
        with app.app_context():
            attended = build_attendance_history(
                player_id=player_id, range_start=_WINDOW[0], range_end=_WINDOW[1]
            )
            missed = build_absence_history(
                player_id=player_id, range_start=_WINDOW[0], range_end=_WINDOW[1]
            )

        attended_ids = {s["lessonInstanceId"] for s in attended["sessions"]}
        missed_ids = {s["lessonInstanceId"] for s in missed["sessions"]}

        assert attended_ids and missed_ids
        assert attended_ids.isdisjoint(missed_ids)
        assert attended["total"] == 2 and missed["total"] == 2

    def test_total_matches_the_dashboard_missed_kpi(self, app):
        """Spec rule 2 — the page and the KPI that links to it cannot disagree.

        This is the assertion that would catch someone "improving" the page by
        filtering out justified absences.
        """
        from padel_app.helpers.dashboard.kpis import compute_player_kpis
        from padel_app.services.attendance_history_service import build_absence_history

        player_id, _ = _seed_history(app)
        with app.app_context():
            kpis = compute_player_kpis(player_id=player_id)
            # A window wide enough to contain every seeded class, so the page
            # total is comparable to the KPI's all-time count.
            payload = build_absence_history(
                player_id=player_id,
                range_start=datetime(2020, 1, 1),
                range_end=datetime(2030, 1, 1),
            )

        assert payload["total"] == kpis.lessons_missed == 2

    def test_justified_absences_are_included_not_filtered(self, app):
        """Spec rule 3 — both justifications count toward the total."""
        from padel_app.services.attendance_history_service import build_absence_history

        player_id, _ = _seed_history(app)
        with app.app_context():
            payload = build_absence_history(
                player_id=player_id, range_start=_WINDOW[0], range_end=_WINDOW[1]
            )

        justifications = sorted(s["justification"] for s in payload["sessions"])
        assert justifications == ["justified", "unjustified"]

    def test_attendance_history_does_not_leak_justification(self, app):
        """`include_justification` is opt-in; the attended payload is unchanged.

        PAD-114's response shape is a shipped contract — widening it here would
        be an unrequested change to a different page's API.
        """
        from padel_app.services.attendance_history_service import build_attendance_history

        player_id, _ = _seed_history(app)
        with app.app_context():
            payload = build_attendance_history(
                player_id=player_id, range_start=_WINDOW[0], range_end=_WINDOW[1]
            )

        assert payload["sessions"]
        for session in payload["sessions"]:
            assert "justification" not in session

    def test_buckets_are_gap_filled_like_the_attendance_page(self, app):
        """Rule 4 — same bucketing contract, so the axis stays continuous."""
        from padel_app.services.attendance_history_service import build_absence_history

        player_id, _ = _seed_history(app)
        with app.app_context():
            payload = build_absence_history(
                player_id=player_id, range_start=_WINDOW[0], range_end=_WINDOW[1]
            )

        assert payload["granularity"] == "day"
        assert len(payload["buckets"]) == 31
        assert sum(b["count"] for b in payload["buckets"]) == 2
        # Empty days are present rather than skipped.
        assert any(b["count"] == 0 for b in payload["buckets"])

    def test_sessions_carry_the_calendar_deep_link(self, app):
        """Rule 5 — always the materialized `lessoninstance-<id>` form."""
        from padel_app.services.attendance_history_service import build_absence_history

        player_id, _ = _seed_history(app)
        with app.app_context():
            payload = build_absence_history(
                player_id=player_id, range_start=_WINDOW[0], range_end=_WINDOW[1]
            )

        for session in payload["sessions"]:
            assert session["href"].startswith("/calendar?classId=lessoninstance-")
            assert "date=" in session["href"]
            assert session["calendarEventId"] == (
                f"lessoninstance-{session['lessonInstanceId']}"
            )

    def test_range_excludes_out_of_window_absences(self, app):
        """A narrow window must not quietly return everything."""
        from padel_app.services.attendance_history_service import build_absence_history

        player_id, _ = _seed_history(app)
        with app.app_context():
            payload = build_absence_history(
                player_id=player_id,
                range_start=datetime(2026, 1, 1),
                range_end=datetime(2026, 1, 31),
            )

        assert payload["total"] == 0
        assert payload["sessions"] == []
