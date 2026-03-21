"""
Shared test helper utilities.
"""
from padel_app.sql_db import db


def make_coach(app) -> int:
    """
    Create a minimal User + Coach in the test DB and return the coach_id.
    Idempotent within a single app context.
    """
    from padel_app.models import User
    from padel_app.models.coach import Coach

    with app.app_context():
        user = User(username="test_coach_helper", password="testpass123")
        db.session.add(user)
        db.session.flush()

        coach = Coach(user_id=user.id)
        db.session.add(coach)
        db.session.commit()
        return coach.id
