from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from werkzeug.security import generate_password_hash

from padel_app.models import (
    Association_CoachClub,
    Association_CoachLesson,
    Association_CoachLessonInstance,
    Association_CoachPlayer,
    Association_PlayerClub,
    Association_PlayerLesson,
    Association_PlayerLessonInstance,
    CalendarBlock,
    Club,
    Coach,
    CoachLevel,
    Conversation,
    ConversationParticipant,
    Exercise,
    ExerciseGroup,
    Lesson,
    LessonInstance,
    Message,
    Player,
    PlayerLevelHistory,
    Presence,
    User,
)
from padel_app.models.Association_CoachExercise import Association_CoachExercise
from padel_app.models.Association_CoachExerciseGroup import Association_CoachExerciseGroup
from padel_app.sql_db import db


DEFAULT_PASSWORD = "test1234"


MOCK_USERS = [
    {
        "name": "Bernardo Terroso",
        "username": "bernardo_terroso",
        "email": "bernardo@academy.pt",
        "phone": "+351900000000",
        "status": "active",
    },
    {
        "name": "Catarina Vilela",
        "username": "catarina_vilela",
        "email": "catarina_vilela@academy.pt",
        "phone": "+351900000010",
        "status": "active",
    },
    {
        "name": "Pedro Pacheco",
        "username": "pedropacheco",
        "email": "pedropacheco@gmail.com",
        "phone": "+351918966340",
        "status": "inactive",
    },
    {
        "name": "Tomas Pacheco",
        "username": "tomaspacheco",
        "email": "tomaspacheco@gmail.com",
        "phone": "+34623456789",
        "status": "active",
    },
    {
        "name": "Bernardo Castro",
        "username": "bernardoc",
        "email": "bernardoc@gmail.com",
        "phone": None,
        "status": "inactive",
    },
    {
        "name": "Dudas BF",
        "username": "dudasbf",
        "email": "dudasbf@gmail.com",
        "phone": "+351911111111",
        "status": "active",
    },
    {
        "name": "Talinho Garrett",
        "username": "talinho",
        "email": "talinho@gmail.com",
        "phone": None,
        "status": "inactive",
    },
    {
        "name": "Antonio Neto",
        "username": "antonioneto",
        "email": "antonioneto@gmail.com",
        "phone": "+351912222222",
        "status": "inactive",
    },
    {
        "name": "Diogo Malafaya",
        "username": "diogom",
        "email": "diogom@gmail.com",
        "phone": None,
        "status": "active",
    },
    {
        "name": "Joao Magalhaes",
        "username": "joaom",
        "email": "joaom@gmail.com",
        "phone": None,
        "status": "inactive",
    },
]

MOCK_CLUBS = [
    {
        "name": "Douro Padel",
        "description": "Main academy club",
        "location": "Porto",
    }
]

MOCK_COACHES = [
    {"username": "bernardo_terroso"},
    {"username": "catarina_vilela"},
]

MOCK_PLAYERS = [
    {"username": "pedropacheco"},
    {"username": "tomaspacheco"},
    {"username": "bernardoc"},
    {"username": "dudasbf"},
    {"username": "talinho"},
    {"username": "antonioneto"},
    {"username": "diogom"},
    {"username": "joaom"},
]

MOCK_COACH_LEVELS = [
    {
        "coach_username": "bernardo_terroso",
        "label": "Beginner",
        "code": "B",
        "display_order": 1,
    },
    {
        "coach_username": "bernardo_terroso",
        "label": "Intermediate",
        "code": "I",
        "display_order": 2,
    },
    {
        "coach_username": "bernardo_terroso",
        "label": "Advanced",
        "code": "A",
        "display_order": 3,
    },
]

MOCK_COACH_IN_CLUB = [
    {"coach_username": "bernardo_terroso", "club_name": "Douro Padel"},
    {"coach_username": "catarina_vilela", "club_name": "Douro Padel"},
]

MOCK_PLAYER_IN_CLUB = [
    {"player_username": "pedropacheco", "club_name": "Douro Padel"},
    {"player_username": "tomaspacheco", "club_name": "Douro Padel"},
    {"player_username": "bernardoc", "club_name": "Douro Padel"},
    {"player_username": "dudasbf", "club_name": "Douro Padel"},
    {"player_username": "talinho", "club_name": "Douro Padel"},
    {"player_username": "antonioneto", "club_name": "Douro Padel"},
    {"player_username": "diogom", "club_name": "Douro Padel"},
    {"player_username": "joaom", "club_name": "Douro Padel"},
]

MOCK_COACH_IN_PLAYER = [
    {
        "coach_username": "bernardo_terroso",
        "player_username": "pedropacheco",
        "level_code": "B",
        "side": "left",
    },
    {
        "coach_username": "bernardo_terroso",
        "player_username": "tomaspacheco",
        "level_code": "I",
        "side": "right",
    },
    {
        "coach_username": "bernardo_terroso",
        "player_username": "bernardoc",
        "level_code": "B",
        "side": "left",
    },
    {
        "coach_username": "bernardo_terroso",
        "player_username": "dudasbf",
        "level_code": "I",
        "side": "right",
    },
    {
        "coach_username": "bernardo_terroso",
        "player_username": "talinho",
        "level_code": "A",
        "side": "left",
    },
    {
        "coach_username": "bernardo_terroso",
        "player_username": "antonioneto",
        "level_code": "A",
        "side": "right",
    },
    {
        "coach_username": "bernardo_terroso",
        "player_username": "diogom",
        "level_code": "I",
        "side": "left",
    },
    {
        "coach_username": "bernardo_terroso",
        "player_username": "joaom",
        "level_code": "B",
        "side": "right",
    },
]


def _dt(days_from_now: int, hour: int, minute: int = 0) -> datetime:
    base = datetime.utcnow().replace(second=0, microsecond=0)
    return (base + timedelta(days=days_from_now)).replace(hour=hour, minute=minute)


MOCK_LESSONS = [
    {
        "title": "Academia Principiantes",
        "description": "Academy for beginners",
        "type": "academy",
        "status": "active",
        "color": "#0ea5e9",
        "max_players": 4,
        "default_level_code": "B",
        "club_name": "Douro Padel",
        "coach_username": "bernardo_terroso",
        "start_datetime": _dt(-2, 9, 0),
        "end_datetime": _dt(-2, 10, 30),
        "is_recurring": True,
        "recurrence_rule": {"frequency": "weekly", "daysOfWeek": [1, 3]},
        "recurrence_end": date.today() + timedelta(days=30),
        "player_usernames": ["pedropacheco", "tomaspacheco", "bernardoc", "dudasbf"],
    },
    {
        "title": "Academia Intermedios",
        "description": "Intermediate group",
        "type": "academy",
        "status": "active",
        "color": "#8b5cf6",
        "max_players": 6,
        "default_level_code": "I",
        "club_name": "Douro Padel",
        "coach_username": "bernardo_terroso",
        "start_datetime": _dt(-1, 17, 0),
        "end_datetime": _dt(-1, 18, 30),
        "is_recurring": True,
        "recurrence_rule": {"frequency": "weekly", "daysOfWeek": [2, 4]},
        "recurrence_end": date.today() + timedelta(days=14),
        "player_usernames": ["tomaspacheco", "talinho", "antonioneto", "diogom"],
    },
    {
        "title": "Aula Privada Antonio",
        "description": "Individual session",
        "type": "private",
        "status": "active",
        "color": "#ec4899",
        "max_players": 1,
        "default_level_code": "A",
        "club_name": "Douro Padel",
        "coach_username": "bernardo_terroso",
        "start_datetime": _dt(5, 9, 0),
        "end_datetime": _dt(5, 10, 0),
        "is_recurring": False,
        "recurrence_rule": None,
        "recurrence_end": None,
        "player_usernames": ["antonioneto"],
    },
]

MOCK_LESSON_INSTANCES = [
    {
        "lesson_title": "Academia Principiantes",
        "start_datetime": _dt(1, 9, 0),
        "end_datetime": _dt(1, 10, 30),
        "status": "scheduled",
        "overwrite_title": None,
    },
    {
        "lesson_title": "Academia Intermedios",
        "start_datetime": _dt(2, 17, 0),
        "end_datetime": _dt(2, 18, 30),
        "status": "scheduled",
        "overwrite_title": None,
    },
]

MOCK_CALENDAR_BLOCKS = [
    {
        "username": "bernardo_terroso",
        "title": "Lunch",
        "description": "Break between sessions",
        "type": "break",
        "start_datetime": _dt(0, 13, 0),
        "end_datetime": _dt(0, 14, 0),
        "is_recurring": True,
        "recurrence_rule": {"frequency": "weekly", "daysOfWeek": [1, 2, 3, 4, 5]},
        "recurrence_end": date.today() + timedelta(days=120),
    },
    {
        "username": "bernardo_terroso",
        "title": "Personal Errand",
        "description": "Administrative tasks",
        "type": "personal",
        "start_datetime": _dt(3, 11, 0),
        "end_datetime": _dt(3, 12, 0),
        "is_recurring": False,
        "recurrence_rule": None,
        "recurrence_end": None,
    },
]

MOCK_CONVERSATIONS = [
    {
        "participants": ["bernardo_terroso", "catarina_vilela"],
        "messages": [
            ("bernardo_terroso", "Hi Catarina, confirming tomorrow lesson.", "2024-01-15T09:00:00"),
            ("catarina_vilela", "Confirmed, 18:00 works for me.", "2024-01-15T09:15:00"),
        ],
    },
    {
        "participants": ["bernardo_terroso", "tomaspacheco"],
        "messages": [
            ("tomaspacheco", "Coach, do we have an extra slot this week?", "2024-01-14T14:00:00"),
            ("bernardo_terroso", "I can do Thursday 17:00.", "2024-01-14T14:30:00"),
        ],
    },
]


MOCK_EXERCISES = [
    {
        "name": "Cross-court Lob Recovery",
        "description": (
            "Player 1 at the net plays a volley, opponent responds with a cross-court lob. "
            "Player 2 recovers behind and plays a bandeja. Focus on positioning and communication."
        ),
        "type": "defense",
        "difficulty": 3,
        "level_ids": [],
        "notes": "Emphasize split-step timing and early ball tracking. Rotate players after 5 reps.",
        "coach_username": "bernardo_terroso",
        "diagram": {
            "elements": [
                {"id": "m1-p1", "type": "player_1", "x": 80, "y": 210, "label": "P1"},
                {"id": "m1-p2", "type": "player_2", "x": 180, "y": 210, "label": "P2"},
                {"id": "m1-p3", "type": "player_3", "x": 90, "y": 330, "label": "P3"},
                {"id": "m1-p4", "type": "player_4", "x": 170, "y": 330, "label": "P4"},
                {"id": "m1-coach", "type": "coach", "x": 40, "y": 270},
                {"id": "m1-ball", "type": "ball", "x": 80, "y": 230},
                {"id": "m1-a1", "type": "arrow", "x": 90, "y": 330, "endX": 160, "endY": 200, "label": "Lob", "curve": -35},
                {"id": "m1-a2", "type": "movement", "x": 180, "y": 210, "endX": 180, "endY": 130, "label": "Recovery", "curve": 20},
                {"id": "m1-a3", "type": "arrow", "x": 180, "y": 130, "endX": 90, "endY": 350, "label": "Bandeja", "curve": 25},
                {"id": "m1-c1", "type": "cone", "x": 120, "y": 180},
                {"id": "m1-c2", "type": "cone", "x": 140, "y": 180},
            ]
        },
    },
    {
        "name": "Serve & Volley Drill",
        "description": (
            "Practice the serve and immediate net approach. Server hits wide, "
            "follows the ball to the net, and finishes with a volley."
        ),
        "type": "serve",
        "difficulty": 2,
        "level_ids": [],
        "notes": None,
        "coach_username": "bernardo_terroso",
        "diagram": {
            "elements": [
                {"id": "m2-p1", "type": "player_1", "x": 80, "y": 460, "label": "P1"},
                {"id": "m2-p3", "type": "player_3", "x": 170, "y": 100, "label": "P3"},
                {"id": "m2-a1", "type": "arrow", "x": 80, "y": 460, "endX": 170, "endY": 120, "label": "Serve", "curve": 15},
                {"id": "m2-a2", "type": "movement", "x": 80, "y": 460, "endX": 90, "endY": 280, "label": "Approach", "curve": -20},
                {"id": "m2-a3", "type": "arrow", "x": 90, "y": 280, "endX": 160, "endY": 350, "label": "Volley", "curve": 0},
            ]
        },
    },
    {
        "name": "Net Approach Volley",
        "description": "Quick volley exchanges at the net. Both players rally volleys cross-court with emphasis on soft hands.",
        "type": "volley",
        "difficulty": 2,
        "level_ids": [],
        "notes": None,
        "coach_username": "bernardo_terroso",
        "diagram": None,
    },
    {
        "name": "Smash & Bandeja Rotation",
        "description": "Coach feeds high balls alternating between smash and bandeja zones. Players rotate after each shot.",
        "type": "attack",
        "difficulty": 4,
        "level_ids": [],
        "notes": None,
        "coach_username": "bernardo_terroso",
        "diagram": None,
    },
]

MOCK_EXERCISE_GROUPS = [
    {
        "name": "Attacking Training at the Net",
        "description": "A series of drills focused on net play, volleys and finishing shots.",
        "coach_username": "bernardo_terroso",
        "exercise_names": ["Serve & Volley Drill", "Net Approach Volley", "Smash & Bandeja Rotation"],
    },
    {
        "name": "Defensive Positioning",
        "description": "Drills for lob recovery and defensive transitions.",
        "coach_username": "bernardo_terroso",
        "exercise_names": ["Cross-court Lob Recovery"],
    },
]


@dataclass
class SeedResult:
    inserted: int = 0
    updated: int = 0


TABLE_DEPENDENCIES = {
    "users": [],
    "clubs": [],
    "coaches": ["users"],
    "players": ["users"],
    "coach_levels": ["coaches"],
    "coach_in_club": ["coaches", "clubs"],
    "player_in_club": ["players", "clubs"],
    "coach_in_player": ["coaches", "players", "coach_levels"],
    "player_level_history": ["coach_in_player"],
    "lessons": ["clubs", "coaches", "coach_levels", "players"],
    "lesson_instances": ["lessons", "coaches", "players"],
    "calendar_blocks": ["users"],
    "conversations": ["users"],
    "messages": ["conversations"],
    "presences": ["lesson_instances", "players"],
    "exercises": ["coaches"],
    "exercise_groups": ["coaches", "exercises"],
}

ALL_SEED_TABLES = [
    "users",
    "clubs",
    "coaches",
    "players",
    "coach_levels",
    "coach_in_club",
    "player_in_club",
    "coach_in_player",
    "player_level_history",
    "lessons",
    "lesson_instances",
    "calendar_blocks",
    "conversations",
    "messages",
    "presences",
    "exercises",
    "exercise_groups",
]


TABLE_ALIASES = {
    "user": "users",
    "users": "users",
    "club": "clubs",
    "clubs": "clubs",
    "coach": "coaches",
    "coaches": "coaches",
    "player": "players",
    "players": "players",
    "coach_level": "coach_levels",
    "coach_levels": "coach_levels",
    "coach_in_club": "coach_in_club",
    "association_coachclub": "coach_in_club",
    "player_in_club": "player_in_club",
    "association_playerclub": "player_in_club",
    "coach_in_player": "coach_in_player",
    "association_coachplayer": "coach_in_player",
    "player_level_history": "player_level_history",
    "lesson": "lessons",
    "lessons": "lessons",
    "coach_in_lesson": "lessons",
    "player_in_lesson": "lessons",
    "lesson_instance": "lesson_instances",
    "lesson_instances": "lesson_instances",
    "coach_in_lesson_instance": "lesson_instances",
    "player_in_lesson_instance": "lesson_instances",
    "calendar_block": "calendar_blocks",
    "calendar_blocks": "calendar_blocks",
    "conversation": "conversations",
    "conversations": "conversations",
    "conversation_participants": "conversations",
    "message": "messages",
    "messages": "messages",
    "presence": "presences",
    "presences": "presences",
    "exercise": "exercises",
    "exercises": "exercises",
    "exercise_group": "exercise_groups",
    "exercise_groups": "exercise_groups",
}


def normalize_table_name(value: str) -> str | None:
    return TABLE_ALIASES.get((value or "").strip().lower())


def available_table_names() -> list[str]:
    return sorted(TABLE_DEPENDENCIES.keys())


def seed_mock_tables(requested_tables: list[str]) -> dict[str, SeedResult]:
    execution_order = _expand_with_dependencies(requested_tables)
    results: dict[str, SeedResult] = {}

    for table_name in execution_order:
        seeder = _SEEDERS[table_name]
        results[table_name] = seeder()

    db.session.commit()
    return results


def _expand_with_dependencies(requested_tables: list[str]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()

    def visit(table_name: str) -> None:
        if table_name in seen:
            return
        for dependency in TABLE_DEPENDENCIES[table_name]:
            visit(dependency)
        seen.add(table_name)
        output.append(table_name)

    for table_name in requested_tables:
        visit(table_name)

    return output


def _user_by_username(username: str) -> User:
    user = User.query.filter_by(username=username).first()
    if not user:
        raise ValueError(f"User not found for username='{username}'")
    return user


def _coach_by_username(username: str) -> Coach:
    user = _user_by_username(username)
    coach = Coach.query.filter_by(user_id=user.id).first()
    if not coach:
        raise ValueError(f"Coach not found for username='{username}'")
    return coach


def _player_by_username(username: str) -> Player:
    user = _user_by_username(username)
    player = Player.query.filter_by(user_id=user.id).first()
    if not player:
        raise ValueError(f"Player not found for username='{username}'")
    return player


def _club_by_name(name: str) -> Club:
    club = Club.query.filter_by(name=name).first()
    if not club:
        raise ValueError(f"Club not found for name='{name}'")
    return club


def _coach_level(coach_id: int, code: str) -> CoachLevel:
    level = CoachLevel.query.filter_by(coach_id=coach_id, code=code).first()
    if not level:
        raise ValueError(f"CoachLevel not found for coach_id={coach_id}, code='{code}'")
    return level


def _seed_users() -> SeedResult:
    result = SeedResult()

    for row in MOCK_USERS:
        user = User.query.filter_by(username=row["username"]).first()
        if not user:
            user = User(
                name=row["name"],
                username=row["username"],
                email=row["email"],
                phone=row["phone"],
                password=generate_password_hash(DEFAULT_PASSWORD),
                status=row["status"],
                is_admin=False,
            )
            db.session.add(user)
            result.inserted += 1
            continue

        changed = False
        if user.name != row["name"]:
            user.name = row["name"]
            changed = True
        if user.email != row["email"]:
            user.email = row["email"]
            changed = True
        if user.phone != row["phone"]:
            user.phone = row["phone"]
            changed = True
        if user.status != row["status"]:
            user.status = row["status"]
            changed = True
        if not user.password:
            user.password = generate_password_hash(DEFAULT_PASSWORD)
            changed = True

        if changed:
            result.updated += 1

    db.session.flush()
    return result


def _seed_clubs() -> SeedResult:
    result = SeedResult()

    for row in MOCK_CLUBS:
        club = Club.query.filter_by(name=row["name"]).first()
        if not club:
            club = Club(
                name=row["name"],
                description=row.get("description"),
                location=row.get("location"),
            )
            db.session.add(club)
            result.inserted += 1
            continue

        changed = False
        if club.description != row.get("description"):
            club.description = row.get("description")
            changed = True
        if club.location != row.get("location"):
            club.location = row.get("location")
            changed = True

        if changed:
            result.updated += 1

    db.session.flush()
    return result


def _seed_coaches() -> SeedResult:
    result = SeedResult()

    for row in MOCK_COACHES:
        user = _user_by_username(row["username"])
        coach = Coach.query.filter_by(user_id=user.id).first()
        if coach:
            continue

        db.session.add(Coach(user_id=user.id))
        result.inserted += 1

    db.session.flush()
    return result


def _seed_players() -> SeedResult:
    result = SeedResult()

    for row in MOCK_PLAYERS:
        user = _user_by_username(row["username"])
        player = Player.query.filter_by(user_id=user.id).first()
        if player:
            continue

        db.session.add(Player(user_id=user.id))
        result.inserted += 1

    db.session.flush()
    return result


def _seed_coach_levels() -> SeedResult:
    result = SeedResult()

    for row in MOCK_COACH_LEVELS:
        coach = _coach_by_username(row["coach_username"])

        level = CoachLevel.query.filter_by(coach_id=coach.id, code=row["code"]).first()
        if not level:
            level = CoachLevel(
                coach_id=coach.id,
                label=row["label"],
                code=row["code"],
                display_order=row["display_order"],
            )
            db.session.add(level)
            result.inserted += 1
            continue

        changed = False
        if level.label != row["label"]:
            level.label = row["label"]
            changed = True
        if level.display_order != row["display_order"]:
            level.display_order = row["display_order"]
            changed = True

        if changed:
            result.updated += 1

    db.session.flush()
    return result


def _seed_coach_in_club() -> SeedResult:
    result = SeedResult()

    for row in MOCK_COACH_IN_CLUB:
        coach = _coach_by_username(row["coach_username"])
        club = _club_by_name(row["club_name"])

        exists = Association_CoachClub.query.filter_by(coach_id=coach.id, club_id=club.id).first()
        if exists:
            continue

        db.session.add(Association_CoachClub(coach_id=coach.id, club_id=club.id))
        result.inserted += 1

    db.session.flush()
    return result


def _seed_player_in_club() -> SeedResult:
    result = SeedResult()

    for row in MOCK_PLAYER_IN_CLUB:
        player = _player_by_username(row["player_username"])
        club = _club_by_name(row["club_name"])

        exists = Association_PlayerClub.query.filter_by(player_id=player.id, club_id=club.id).first()
        if exists:
            continue

        db.session.add(Association_PlayerClub(player_id=player.id, club_id=club.id))
        result.inserted += 1

    db.session.flush()
    return result


def _seed_coach_in_player() -> SeedResult:
    result = SeedResult()

    for row in MOCK_COACH_IN_PLAYER:
        coach = _coach_by_username(row["coach_username"])
        player = _player_by_username(row["player_username"])
        level = _coach_level(coach.id, row["level_code"])

        rel = Association_CoachPlayer.query.filter_by(coach_id=coach.id, player_id=player.id).first()
        if not rel:
            rel = Association_CoachPlayer(
                coach_id=coach.id,
                player_id=player.id,
                level_id=level.id,
                side=row.get("side"),
            )
            db.session.add(rel)
            result.inserted += 1
            continue

        changed = False
        if rel.level_id != level.id:
            rel.level_id = level.id
            changed = True
        if rel.side != row.get("side"):
            rel.side = row.get("side")
            changed = True

        if changed:
            result.updated += 1

    db.session.flush()
    return result


def _seed_player_level_history() -> SeedResult:
    result = SeedResult()

    for row in MOCK_COACH_IN_PLAYER:
        coach = _coach_by_username(row["coach_username"])
        player = _player_by_username(row["player_username"])
        level = _coach_level(coach.id, row["level_code"])

        exists = (
            PlayerLevelHistory.query.filter_by(
                coach_id=coach.id,
                player_id=player.id,
                level_id=level.id,
            )
            .order_by(PlayerLevelHistory.assigned_at.desc())
            .first()
        )
        if exists:
            continue

        db.session.add(
            PlayerLevelHistory(
                coach_id=coach.id,
                player_id=player.id,
                level_id=level.id,
                assigned_at=datetime.utcnow(),
            )
        )
        result.inserted += 1

    db.session.flush()
    return result


def _seed_lessons() -> SeedResult:
    result = SeedResult()

    for row in MOCK_LESSONS:
        coach = _coach_by_username(row["coach_username"])
        club = _club_by_name(row["club_name"])
        level = _coach_level(coach.id, row["default_level_code"])

        lesson = Lesson.query.filter_by(title=row["title"], club_id=club.id).first()
        recurrence_rule = json.dumps(row["recurrence_rule"]) if row.get("recurrence_rule") else None

        if not lesson:
            lesson = Lesson(
                title=row["title"],
                description=row.get("description"),
                start_datetime=row["start_datetime"],
                end_datetime=row["end_datetime"],
                is_recurring=row["is_recurring"],
                recurrence_rule=recurrence_rule,
                recurrence_end=row.get("recurrence_end"),
                type=row["type"],
                default_level_id=level.id,
                max_players=row["max_players"],
                color=row.get("color"),
                status=row.get("status", "active"),
                club_id=club.id,
            )
            db.session.add(lesson)
            db.session.flush()
            result.inserted += 1
        else:
            changed = False
            updates = {
                "description": row.get("description"),
                "start_datetime": row["start_datetime"],
                "end_datetime": row["end_datetime"],
                "is_recurring": row["is_recurring"],
                "recurrence_rule": recurrence_rule,
                "recurrence_end": row.get("recurrence_end"),
                "type": row["type"],
                "default_level_id": level.id,
                "max_players": row["max_players"],
                "color": row.get("color"),
                "status": row.get("status", "active"),
            }
            for field, expected in updates.items():
                if getattr(lesson, field) != expected:
                    setattr(lesson, field, expected)
                    changed = True

            if changed:
                result.updated += 1

        coach_rel = Association_CoachLesson.query.filter_by(coach_id=coach.id, lesson_id=lesson.id).first()
        if not coach_rel:
            db.session.add(Association_CoachLesson(coach_id=coach.id, lesson_id=lesson.id))
            result.inserted += 1

        for player_username in row["player_usernames"]:
            player = _player_by_username(player_username)
            player_rel = Association_PlayerLesson.query.filter_by(
                player_id=player.id,
                lesson_id=lesson.id,
            ).first()
            if not player_rel:
                db.session.add(Association_PlayerLesson(player_id=player.id, lesson_id=lesson.id))
                result.inserted += 1

    db.session.flush()
    return result


def _seed_lesson_instances() -> SeedResult:
    result = SeedResult()

    for row in MOCK_LESSON_INSTANCES:
        lesson = Lesson.query.filter_by(title=row["lesson_title"]).first()
        if not lesson:
            raise ValueError(f"Lesson not found for title='{row['lesson_title']}'")

        instance = LessonInstance.query.filter_by(
            lesson_id=lesson.id,
            start_datetime=row["start_datetime"],
        ).first()

        if not instance:
            instance = LessonInstance(
                lesson_id=lesson.id,
                original_lesson_occurence_date=row["start_datetime"].date(),
                start_datetime=row["start_datetime"],
                end_datetime=row["end_datetime"],
                overwrite_title=row.get("overwrite_title"),
                level_id=lesson.default_level_id,
                status=row.get("status", "scheduled"),
                max_players=lesson.max_players,
            )
            db.session.add(instance)
            db.session.flush()
            result.inserted += 1
        else:
            changed = False
            updates = {
                "end_datetime": row["end_datetime"],
                "overwrite_title": row.get("overwrite_title"),
                "status": row.get("status", "scheduled"),
                "level_id": lesson.default_level_id,
                "max_players": lesson.max_players,
            }
            for field, expected in updates.items():
                if getattr(instance, field) != expected:
                    setattr(instance, field, expected)
                    changed = True

            if changed:
                result.updated += 1

        coach_links = Association_CoachLesson.query.filter_by(lesson_id=lesson.id).all()
        for coach_link in coach_links:
            existing = Association_CoachLessonInstance.query.filter_by(
                coach_id=coach_link.coach_id,
                lesson_instance_id=instance.id,
            ).first()
            if not existing:
                db.session.add(
                    Association_CoachLessonInstance(
                        coach_id=coach_link.coach_id,
                        lesson_instance_id=instance.id,
                    )
                )
                result.inserted += 1

        player_links = Association_PlayerLesson.query.filter_by(lesson_id=lesson.id).all()
        for player_link in player_links:
            existing = Association_PlayerLessonInstance.query.filter_by(
                player_id=player_link.player_id,
                lesson_instance_id=instance.id,
            ).first()
            if not existing:
                db.session.add(
                    Association_PlayerLessonInstance(
                        player_id=player_link.player_id,
                        lesson_instance_id=instance.id,
                    )
                )
                result.inserted += 1

    db.session.flush()
    return result


def _seed_calendar_blocks() -> SeedResult:
    result = SeedResult()

    for row in MOCK_CALENDAR_BLOCKS:
        user = _user_by_username(row["username"])

        block = CalendarBlock.query.filter_by(
            user_id=user.id,
            title=row["title"],
            start_datetime=row["start_datetime"],
        ).first()

        recurrence_rule = json.dumps(row["recurrence_rule"]) if row.get("recurrence_rule") else None

        if not block:
            db.session.add(
                CalendarBlock(
                    user_id=user.id,
                    title=row["title"],
                    description=row.get("description"),
                    type=row["type"],
                    start_datetime=row["start_datetime"],
                    end_datetime=row["end_datetime"],
                    is_recurring=row["is_recurring"],
                    recurrence_rule=recurrence_rule,
                    recurrence_end=row.get("recurrence_end"),
                )
            )
            result.inserted += 1
            continue

        changed = False
        updates = {
            "description": row.get("description"),
            "type": row["type"],
            "end_datetime": row["end_datetime"],
            "is_recurring": row["is_recurring"],
            "recurrence_rule": recurrence_rule,
            "recurrence_end": row.get("recurrence_end"),
        }
        for field, expected in updates.items():
            if getattr(block, field) != expected:
                setattr(block, field, expected)
                changed = True

        if changed:
            result.updated += 1

    db.session.flush()
    return result


def _seed_conversations() -> SeedResult:
    result = SeedResult()

    for row in MOCK_CONVERSATIONS:
        users = [_user_by_username(username) for username in row["participants"]]
        participant_ids = [u.id for u in users]
        participant_key = Conversation.build_participant_key(participant_ids)

        conversation = Conversation.query.filter_by(participant_key=participant_key).first()
        if not conversation:
            conversation = Conversation(
                is_group=False,
                group_name=None,
                participant_key=participant_key,
            )
            db.session.add(conversation)
            db.session.flush()
            result.inserted += 1

        for user in users:
            participant = ConversationParticipant.query.filter_by(
                conversation_id=conversation.id,
                user_id=user.id,
            ).first()
            if participant:
                continue

            db.session.add(
                ConversationParticipant(
                    conversation_id=conversation.id,
                    user_id=user.id,
                )
            )
            result.inserted += 1

    db.session.flush()
    return result


def _seed_messages() -> SeedResult:
    result = SeedResult()

    for row in MOCK_CONVERSATIONS:
        users = [_user_by_username(username) for username in row["participants"]]
        participant_key = Conversation.build_participant_key([u.id for u in users])
        conversation = Conversation.query.filter_by(participant_key=participant_key).first()
        if not conversation:
            raise ValueError(f"Conversation not found for participant_key='{participant_key}'")

        for sender_username, text, sent_at in row["messages"]:
            sender = _user_by_username(sender_username)
            sent_at_dt = datetime.fromisoformat(sent_at)

            existing = Message.query.filter_by(
                conversation_id=conversation.id,
                sender_id=sender.id,
                text=text,
                sent_at=sent_at_dt,
            ).first()
            if existing:
                continue

            db.session.add(
                Message(
                    conversation_id=conversation.id,
                    sender_id=sender.id,
                    text=text,
                    sent_at=sent_at_dt,
                )
            )
            result.inserted += 1

    db.session.flush()
    return result


def _seed_presences() -> SeedResult:
    result = SeedResult()

    links = Association_PlayerLessonInstance.query.all()
    grouped: dict[int, list[Association_PlayerLessonInstance]] = defaultdict(list)
    for link in links:
        grouped[link.lesson_instance_id].append(link)

    for lesson_instance_id, rows in grouped.items():
        sorted_rows = sorted(rows, key=lambda item: item.player_id)
        for index, row in enumerate(sorted_rows):
            presence = Presence.query.filter_by(
                lesson_instance_id=lesson_instance_id,
                player_id=row.player_id,
            ).first()
            status = "present" if index % 2 == 0 else "absent"
            justification = "justified" if status == "absent" else None

            if not presence:
                db.session.add(
                    Presence(
                        lesson_instance_id=lesson_instance_id,
                        player_id=row.player_id,
                        status=status,
                        justification=justification,
                        invited=True,
                        confirmed=status == "present",
                        validated=False,
                    )
                )
                result.inserted += 1
                continue

            changed = False
            updates = {
                "status": status,
                "justification": justification,
                "invited": True,
                "confirmed": status == "present",
            }
            for field, expected in updates.items():
                if getattr(presence, field) != expected:
                    setattr(presence, field, expected)
                    changed = True

            if changed:
                result.updated += 1

    db.session.flush()
    return result


def _seed_exercises() -> SeedResult:
    result = SeedResult()

    for row in MOCK_EXERCISES:
        coach = _coach_by_username(row["coach_username"])

        exercise = Exercise.query.filter_by(name=row["name"], owner_coach_id=coach.id).first()
        if not exercise:
            exercise = Exercise(
                name=row["name"],
                description=row.get("description"),
                type=row["type"],
                difficulty=row["difficulty"],
                level_ids=row.get("level_ids", []),
                notes=row.get("notes"),
                diagram=row.get("diagram"),
                owner_coach_id=coach.id,
            )
            db.session.add(exercise)
            db.session.flush()

            db.session.add(Association_CoachExercise(
                coach_id=coach.id,
                exercise_id=exercise.id,
                role="owner",
            ))
            result.inserted += 1
            continue

        changed = False
        updates = {
            "description": row.get("description"),
            "type": row["type"],
            "difficulty": row["difficulty"],
            "level_ids": row.get("level_ids", []),
            "notes": row.get("notes"),
            "diagram": row.get("diagram"),
        }
        for field, expected in updates.items():
            if getattr(exercise, field) != expected:
                setattr(exercise, field, expected)
                changed = True

        if changed:
            result.updated += 1

    db.session.flush()
    return result


def _seed_exercise_groups() -> SeedResult:
    result = SeedResult()

    for row in MOCK_EXERCISE_GROUPS:
        coach = _coach_by_username(row["coach_username"])

        group = ExerciseGroup.query.filter_by(name=row["name"], owner_coach_id=coach.id).first()
        if not group:
            group = ExerciseGroup(
                name=row["name"],
                description=row.get("description"),
                owner_coach_id=coach.id,
            )
            db.session.add(group)
            db.session.flush()

            db.session.add(Association_CoachExerciseGroup(
                coach_id=coach.id,
                exercise_group_id=group.id,
                role="owner",
            ))
            result.inserted += 1
        else:
            changed = False
            if group.description != row.get("description"):
                group.description = row.get("description")
                changed = True
            if changed:
                result.updated += 1

        # Sync exercises in the group
        for exercise_name in row["exercise_names"]:
            exercise = Exercise.query.filter_by(name=exercise_name, owner_coach_id=coach.id).first()
            if exercise and exercise not in group.exercises:
                group.exercises.append(exercise)

    db.session.flush()
    return result


_SEEDERS = {
    "users": _seed_users,
    "clubs": _seed_clubs,
    "coaches": _seed_coaches,
    "players": _seed_players,
    "coach_levels": _seed_coach_levels,
    "coach_in_club": _seed_coach_in_club,
    "player_in_club": _seed_player_in_club,
    "coach_in_player": _seed_coach_in_player,
    "player_level_history": _seed_player_level_history,
    "lessons": _seed_lessons,
    "lesson_instances": _seed_lesson_instances,
    "calendar_blocks": _seed_calendar_blocks,
    "conversations": _seed_conversations,
    "messages": _seed_messages,
    "presences": _seed_presences,
    "exercises": _seed_exercises,
    "exercise_groups": _seed_exercise_groups,
}
