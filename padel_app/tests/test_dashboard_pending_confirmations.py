"""
PAD-78: pending-confirmations count + manual-notify targeting.

A student is "pending confirmation" for a tomorrow class when they have a
NotificationEvent still in the ``sent`` state (invited/notified, but neither
confirmed -> ``confirmed`` nor declined/timed-out -> ``expired``).
"""
from datetime import datetime, timedelta


def _seed(app):
    from padel_app.sql_db import db
    from padel_app.models import User, LessonInstance, NotificationEvent
    from padel_app.models.coaches import Coach
    from padel_app.models.players import Player
    from padel_app.models.clubs import Club
    from padel_app.models.lessons import Lesson
    from padel_app.models.Association_CoachClub import Association_CoachClub

    with app.app_context():
        coach_user = User(name="C", username="pc_coach", password="x")
        db.session.add(coach_user)
        db.session.flush()
        coach = Coach(user_id=coach_user.id)
        db.session.add(coach)
        db.session.flush()

        club = Club(name="PC Club", description="d", location="l")
        db.session.add(club)
        db.session.flush()
        db.session.add(Association_CoachClub(coach_id=coach.id, club_id=club.id))

        players = []
        for i in range(4):
            u = User(name=f"P{i}", username=f"pc_p{i}", password="x")
            db.session.add(u)
            db.session.flush()
            p = Player(user_id=u.id)
            db.session.add(p)
            db.session.flush()
            players.append(p)

        def make_instance(start):
            lesson = Lesson(
                title="C",
                start_datetime=start,
                end_datetime=start + timedelta(hours=1),
                is_recurring=False,
                type="academy",
                max_players=6,
                status="active",
                club_id=club.id,
            )
            db.session.add(lesson)
            db.session.flush()
            inst = LessonInstance(
                lesson_id=lesson.id,
                start_datetime=start,
                end_datetime=start + timedelta(hours=1),
                max_players=6,
                status="scheduled",
                notifications_enabled=True,
                original_lesson_occurence_date=start.date(),
            )
            db.session.add(inst)
            db.session.flush()
            return inst

        now = datetime.utcnow()
        tomorrow = (now + timedelta(days=1)).replace(hour=18, minute=0, second=0, microsecond=0)
        today = now.replace(hour=9, minute=0, second=0, microsecond=0)

        tomo_inst = make_instance(tomorrow)
        today_inst = make_instance(today)

        # Tomorrow: p0 & p1 pending (sent), p2 confirmed, p3 expired/declined.
        db.session.add_all([
            NotificationEvent(coach_id=coach.id, lesson_instance_id=tomo_inst.id,
                              player_id=players[0].id, type="auto", status="sent"),
            NotificationEvent(coach_id=coach.id, lesson_instance_id=tomo_inst.id,
                              player_id=players[1].id, type="auto", status="sent"),
            NotificationEvent(coach_id=coach.id, lesson_instance_id=tomo_inst.id,
                              player_id=players[2].id, type="auto", status="confirmed"),
            NotificationEvent(coach_id=coach.id, lesson_instance_id=tomo_inst.id,
                              player_id=players[3].id, type="auto", status="expired"),
        ])
        # Today's class has a pending event too — must NOT be counted (not tomorrow).
        db.session.add(
            NotificationEvent(coach_id=coach.id, lesson_instance_id=today_inst.id,
                              player_id=players[0].id, type="auto", status="sent")
        )
        db.session.commit()

        return coach.id, tomo_inst.id, {p.id for p in players}


def test_count_pending_confirmations_only_tomorrow_sent(app):
    from padel_app.helpers.dashboard.pending import count_pending_confirmations

    coach_id, _, _ = _seed(app)
    with app.app_context():
        # Only p0 & p1 (sent, tomorrow) count — confirmed/expired/today excluded.
        assert count_pending_confirmations(coach_id=coach_id) == 2


def test_pending_targets_grouped_by_instance(app):
    from padel_app.helpers.dashboard.pending import get_pending_confirmation_targets

    coach_id, tomo_inst_id, _ = _seed(app)
    with app.app_context():
        targets = get_pending_confirmation_targets(coach_id=coach_id)
        assert len(targets) == 1
        instance_id, player_ids = targets[0]
        assert instance_id == tomo_inst_id
        assert len(player_ids) == 2


def test_notify_pending_only_targets_pending_students(app, monkeypatch):
    import padel_app.services.notification_service as ns
    from padel_app.helpers.dashboard import pending as pending_mod

    coach_id, tomo_inst_id, _ = _seed(app)

    calls = []

    def fake_send(instance_id, player_ids, cid):
        calls.append((instance_id, sorted(player_ids), cid))
        # mimic the real return shape (one event per player)
        return [object() for _ in player_ids]

    monkeypatch.setattr(ns, "send_manual_notifications", fake_send)

    with app.app_context():
        result = pending_mod.notify_pending_confirmations(coach_id=coach_id)

    assert result == {"instances": 1, "sent": 2}
    assert len(calls) == 1
    instance_id, player_ids, cid = calls[0]
    assert instance_id == tomo_inst_id
    assert cid == coach_id
    assert len(player_ids) == 2
