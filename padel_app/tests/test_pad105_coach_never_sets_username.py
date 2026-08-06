"""PAD-105 — a coach never chooses or changes a player's username.

A username is a login credential, so it belongs to whoever logs in. Coach-side
player creation assigns a generated placeholder instead, which the player
replaces when they activate their own account (`players.invite-completion` or
`auth.activate`). These tests pin the service layer, so the guarantee holds no
matter which client calls in — including one that still sends a `username`.
"""

import pytest

from padel_app.sql_db import db
from padel_app.tools.username_tools import (
    is_placeholder_username,
    unique_placeholder_username,
)
from padel_app.tests.helpers import make_coach


@pytest.fixture
def coach_id(app):
    return make_coach(app)


def _add_player(app, coach_id, **overrides):
    from padel_app.services.player_service import add_player_service

    payload = {"coachId": coach_id, "name": "Nameless Player"}
    payload.update(overrides)

    with app.app_context():
        return add_player_service(payload)


def _user_for(app, player_info):
    from padel_app.models import Player

    with app.app_context():
        return Player.query.get(player_info["playerId"]).user


def test_creating_a_player_without_a_username_succeeds(app, coach_id):
    """The coach form no longer sends a username; creation must still work."""
    info = _add_player(app, coach_id)

    user = _user_for(app, info)
    assert user.username, "users.username is NOT NULL — a placeholder is required"
    assert is_placeholder_username(user.username)


def test_a_coach_supplied_username_is_ignored(app, coach_id):
    """An older client (or a hand-rolled request) cannot claim a username."""
    info = _add_player(app, coach_id, username="chosen-by-coach")

    user = _user_for(app, info)
    assert user.username != "chosen-by-coach"
    assert is_placeholder_username(user.username)


def test_placeholder_usernames_are_unique_across_players(app, coach_id):
    """Two coach-created players must not collide on the UNIQUE constraint."""
    first = _add_player(app, coach_id, name="Player One")
    second = _add_player(app, coach_id, name="Player Two")

    assert _user_for(app, first).username != _user_for(app, second).username


def test_placeholder_accounts_have_no_password(app, coach_id):
    """A placeholder is not a usable account until the player activates it."""
    info = _add_player(app, coach_id)

    assert _user_for(app, info).password is None


def test_editing_a_player_cannot_change_their_username(app, coach_id):
    """A coach editing a player must not be able to overwrite the username."""
    from padel_app.services.player_service import edit_player_service

    info = _add_player(app, coach_id)
    original_username = _user_for(app, info).username

    with app.app_context():
        edit_player_service(
            {
                "player": {
                    "playerId": info["playerId"],
                    "coachId": coach_id,
                    "name": "Nameless Player",
                },
                "updates": {
                    "name": "Renamed Player",
                    "username": "coach-tried-this",
                },
            }
        )

    user = _user_for(app, info)
    assert user.name == "Renamed Player", "other edits must still apply"
    assert user.username == original_username


def test_unique_placeholder_username_skips_taken_values(app):
    """The generator never hands back a username someone already holds."""
    from padel_app.models import User

    with app.app_context():
        taken = unique_placeholder_username()
        db.session.add(User(name="Taken", username=taken))
        db.session.commit()

        assert unique_placeholder_username() != taken


def test_registration_form_is_not_prefilled_with_the_placeholder(app, coach_id, client):
    """The activation form is where the user PICKS a username.

    Handing back the generated placeholder prefills their username box with
    `pending-<hex>`, which leaks an internal detail and invites them to keep a
    machine-generated login. The field must come back empty instead.
    """
    from padel_app.models import Player

    info = _add_player(app, coach_id)

    with app.app_context():
        user_id = Player.query.get(info["playerId"]).user_id

    res = client.get(f"/api/app/register/user/{user_id}")

    assert res.status_code == 200
    assert res.get_json()["username"] is None


def test_registration_form_keeps_a_real_username(app, client):
    """A user who already chose a username still sees it prefilled."""
    from padel_app.models import User

    with app.app_context():
        user = User(name="Chose Already", username="chosen-by-me")
        db.session.add(user)
        db.session.commit()
        user_id = user.id

    res = client.get(f"/api/app/register/user/{user_id}")

    assert res.get_json()["username"] == "chosen-by-me"
