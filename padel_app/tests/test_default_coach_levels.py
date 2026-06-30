def test_new_coach_gets_default_levels_via_service(app):
    """A coach created through create_coach_service gets L1/L2/L3 levels."""
    from padel_app.sql_db import db
    from padel_app.models import User, CoachLevel
    from padel_app.services.coach_service import create_coach_service

    with app.app_context():
        user = User(name="New Coach", username="new_coach", password="pw123")
        db.session.add(user)
        db.session.commit()

        coach = create_coach_service({"user": user.id})

        levels = CoachLevel.query.filter_by(coach_id=coach.id).order_by(
            CoachLevel.display_order
        ).all()

        assert len(levels) == 3
        assert {l.code for l in levels} == {"L1", "L2", "L3"}
        assert [l.code for l in levels] == ["L1", "L2", "L3"]
        assert [l.label for l in levels] == ["Level 1", "Level 2", "Level 3"]
        assert [l.display_order for l in levels] == [1, 2, 3]


def test_create_default_levels_is_idempotent(app):
    """Calling the helper twice does not create duplicate levels."""
    from padel_app.sql_db import db
    from padel_app.models import User, Coach, CoachLevel
    from padel_app.services.coach_service import create_default_levels_for_coach

    with app.app_context():
        user = User(name="Idem Coach", username="idem_coach", password="pw123")
        db.session.add(user)
        db.session.flush()
        coach = Coach(user_id=user.id)
        db.session.add(coach)
        db.session.commit()

        create_default_levels_for_coach(coach)
        create_default_levels_for_coach(coach)

        levels = CoachLevel.query.filter_by(coach_id=coach.id).all()
        assert len(levels) == 3
        assert {l.code for l in levels} == {"L1", "L2", "L3"}
