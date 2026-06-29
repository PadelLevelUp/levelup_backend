"""
Regression tests locking in the fix that serializes a player's ``levelId`` as a
string (matching how ``CoachLevel.id`` is serialized), so the frontend level
``<Select>`` can match the option value.

See:
- padel_app/services/player_service.py :: _serialize_coach_player_relation
- padel_app/models/players.py :: Player.coach_player_info
"""
from padel_app.sql_db import db
from padel_app.tests.helpers import make_coach


def _make_player_with_relation(coach_id, *, username="lvl_player"):
    """Create a User + Player + Association_CoachPlayer (no level) and return ids."""
    from padel_app.models import User, Player, Association_CoachPlayer

    user = User(
        name="Level Player",
        username=username,
        email=f"{username}@example.com",
        status="active",
    )
    db.session.add(user)
    db.session.flush()

    player = Player(user_id=user.id)
    db.session.add(player)
    db.session.flush()

    rel = Association_CoachPlayer(coach_id=coach_id, player_id=player.id)
    db.session.add(rel)
    db.session.commit()

    return player.id, rel.id


def _make_level(coach_id, *, label="A1", code="A1", display_order=0):
    from padel_app.models.coach_levels import CoachLevel

    level = CoachLevel(
        coach_id=coach_id,
        label=label,
        code=code,
        display_order=display_order,
    )
    db.session.add(level)
    db.session.commit()
    return level.id


def test_edit_player_sets_level_id_as_string(app):
    """edit_player_service must return levelId as str(level.id), matching the
    type used to serialize CoachLevel.id."""
    coach_id = make_coach(app)

    with app.app_context():
        from padel_app.services.player_service import edit_player_service

        level_id = _make_level(coach_id)
        player_id, _ = _make_player_with_relation(coach_id)

        data = {
            "player": {
                "coachId": coach_id,
                "playerId": player_id,
                "levelId": None,
                "side": None,
                "notes": None,
                "name": "Level Player",
                "username": "lvl_player",
                "email": "lvl_player@example.com",
                "phone": None,
            },
            "updates": {
                "levelId": str(level_id),
            },
        }

        result = edit_player_service(data)

        assert result["levelId"] == str(level_id)
        assert isinstance(result["levelId"], str)

        # The id type must match CoachLevel.id serialization (also a string).
        from padel_app.services.player_service import get_coach_players_list
        from padel_app.models.coaches import Coach

        coach = Coach.query.get(coach_id)
        serialized = get_coach_players_list(coach)
        assert len(serialized) == 1
        row = serialized[0]
        assert row["levelId"] == str(level_id)
        assert isinstance(row["levelId"], str)
        assert row["level"]["id"] == str(level_id)
        assert isinstance(row["level"]["id"], str)
        # Both fields carry the same string value -> frontend Select can match.
        assert row["levelId"] == row["level"]["id"]


def test_player_without_level_serializes_none(app):
    """A player with no level must serialize levelId as None (not a string)."""
    coach_id = make_coach(app)

    with app.app_context():
        from padel_app.services.player_service import get_coach_players_list
        from padel_app.models.coaches import Coach

        player_id, _ = _make_player_with_relation(coach_id, username="no_level")

        coach = Coach.query.get(coach_id)
        serialized = get_coach_players_list(coach)
        assert len(serialized) == 1
        row = serialized[0]
        assert row["levelId"] is None
        assert "level" not in row

        # Same expectation via the model serializer used elsewhere.
        from padel_app.models import Player

        player = Player.query.get(player_id)
        info = player.coach_player_info(coach_id)
        assert info["levelId"] is None
