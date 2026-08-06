"""
PAD-109: GET /api/app/notify/player_search — type-ahead student search used by
Settings > Notifications (standing/permanent waiting list + excluded players).

The route did not exist at all, so the frontend search box silently returned
nothing (the 404 was swallowed by the client's `catch`). These tests pin the
contract the frontend depends on:

  - results are scoped to the calling coach's own roster
  - matching is a case-insensitive substring of User.name
  - the returned id is the **Player.id** (what POST /standing_waiting_list and
    restrictions.excludedPlayers.playerIds expect), never the User.id
  - a blank query returns an empty list, not the whole roster

Run:
    pytest padel_app/tests/test_player_search.py -v
"""
import pytest
from flask_jwt_extended import create_access_token

from padel_app.sql_db import db


@pytest.fixture(autouse=True)
def _jwt_secret(app):
    app.config["JWT_SECRET_KEY"] = "test-jwt-secret"


def _auth_header(app, user_id):
    with app.app_context():
        token = create_access_token(identity=str(user_id))
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def world(app):
    """Two coaches with separate rosters, so scoping can be verified."""
    from padel_app.models import User
    from padel_app.models.coaches import Coach
    from padel_app.models.players import Player
    from padel_app.models.Association_CoachPlayer import Association_CoachPlayer

    with app.app_context():
        def mk_user(name, username, status="active"):
            u = User(name=name, username=username, email=f"{username}@test.com",
                     password="hashed", status=status)
            db.session.add(u)
            db.session.flush()
            return u

        coach_a_user = mk_user("Coach A", "coach_a")
        coach_b_user = mk_user("Coach B", "coach_b")
        coach_a = Coach(user_id=coach_a_user.id)
        coach_b = Coach(user_id=coach_b_user.id)
        db.session.add_all([coach_a, coach_b])
        db.session.flush()

        # Coach A's roster
        alice_user = mk_user("Alice Andrade", "alice")
        alberto_user = mk_user("Alberto Basto", "alberto")
        # Inactive (invited, never registered) — still addable to a waiting list
        ghost_user = mk_user("Alva Ghost", "ghost", status="inactive")
        # Coach B's roster — must never surface in Coach A's search
        mallory_user = mk_user("Alice Mallory", "mallory")

        players = {}
        for key, u in [("alice", alice_user), ("alberto", alberto_user),
                       ("ghost", ghost_user), ("mallory", mallory_user)]:
            p = Player(user_id=u.id)
            db.session.add(p)
            db.session.flush()
            players[key] = p

        for key in ("alice", "alberto", "ghost"):
            db.session.add(Association_CoachPlayer(
                coach_id=coach_a.id, player_id=players[key].id
            ))
        db.session.add(Association_CoachPlayer(
            coach_id=coach_b.id, player_id=players["mallory"].id
        ))
        db.session.commit()

        return {
            "coach_a_user_id": coach_a_user.id,
            "coach_b_user_id": coach_b_user.id,
            "coach_a_id": coach_a.id,
            "player_ids": {k: v.id for k, v in players.items()},
            "user_ids": {
                "alice": alice_user.id,
                "alberto": alberto_user.id,
                "ghost": ghost_user.id,
                "mallory": mallory_user.id,
            },
        }


def test_search_filters_by_substring(app, client, world):
    res = client.get("/api/app/notify/player_search?q=alb",
                     headers=_auth_header(app, world["coach_a_user_id"]))
    assert res.status_code == 200
    names = [p["name"] for p in res.get_json()["players"]]
    assert names == ["Alberto Basto"]


def test_search_is_case_insensitive_and_returns_all_matches(app, client, world):
    res = client.get("/api/app/notify/player_search?q=AL",
                     headers=_auth_header(app, world["coach_a_user_id"]))
    names = [p["name"] for p in res.get_json()["players"]]
    # Sorted by name; inactive players are included
    assert names == ["Alberto Basto", "Alice Andrade", "Alva Ghost"]


def test_search_is_scoped_to_the_calling_coach(app, client, world):
    res = client.get("/api/app/notify/player_search?q=Alice",
                     headers=_auth_header(app, world["coach_a_user_id"]))
    names = [p["name"] for p in res.get_json()["players"]]
    assert names == ["Alice Andrade"]
    assert "Alice Mallory" not in names


def test_returned_id_is_the_player_id_not_the_user_id(app, client, world):
    res = client.get("/api/app/notify/player_search?q=Alice Andrade",
                     headers=_auth_header(app, world["coach_a_user_id"]))
    player = res.get_json()["players"][0]
    assert player["id"] == str(world["player_ids"]["alice"])
    # Guard against the classic mix-up
    assert player["id"] != str(world["user_ids"]["alice"])


@pytest.mark.parametrize("q", ["", "   ", "%", "_"])
def test_blank_or_wildcard_query_does_not_dump_the_roster(app, client, world, q):
    res = client.get("/api/app/notify/player_search", query_string={"q": q},
                     headers=_auth_header(app, world["coach_a_user_id"]))
    assert res.status_code == 200
    assert res.get_json()["players"] == []


def test_no_match_returns_empty_list(app, client, world):
    res = client.get("/api/app/notify/player_search?q=zzzz",
                     headers=_auth_header(app, world["coach_a_user_id"]))
    assert res.get_json()["players"] == []


def test_requires_authentication(client):
    res = client.get("/api/app/notify/player_search?q=a")
    assert res.status_code == 401


def test_non_coach_is_forbidden(app, client, world):
    """A user with no Coach record cannot enumerate players."""
    from padel_app.models import User

    with app.app_context():
        plain = User(name="Plain User", username="plain",
                     email="plain@test.com", password="hashed", status="active")
        db.session.add(plain)
        db.session.commit()
        plain_id = plain.id

    res = client.get("/api/app/notify/player_search?q=a",
                     headers=_auth_header(app, plain_id))
    assert res.status_code == 403
