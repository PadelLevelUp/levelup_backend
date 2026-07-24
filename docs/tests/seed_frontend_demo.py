"""Dev-only demo seeder.

PAD-92: this script used to POST anonymously to the `/api/app/*` entity-creation
routes and to `/api/delete/<model>/<id>`. Those `/api/app/*` shortcuts have been
deleted (they were unauthenticated) and the generic CRUD API requires an admin
JWT since PAD-88, so everything now goes through `_seed_auth`, authenticated as
an administrator. Export SEED_ADMIN_USERNAME / SEED_ADMIN_PASSWORD first.
"""
import json
from datetime import datetime, timedelta
from dateutil.relativedelta import relativedelta

from _seed_auth import admin_session, create as api_create, delete as api_delete

PASSWORD = "test1234"

SESSION = admin_session()

created_objects = []

def track(model, obj):
    """
    model: string used by /delete/<model>/<id>
    obj: API response containing 'id'
    """
    created_objects.append((model, obj["id"]))
    return obj

def cleanup():
    print("\n⚠️  Cleaning up created objects...")
    for model, obj_id in reversed(created_objects):
        try:
            api_delete(SESSION, model, obj_id)
            print(f"Deleted {model} {obj_id}")
        except Exception as e:
            print(f"Failed to delete {model} {obj_id}: {e}")

def post(model, payload):
    """Create an entity through the admin CRUD API."""
    return api_create(SESSION, model, payload)

def today():
    return datetime.utcnow().date()

def dt(date, time_str):
    return f"{date.isoformat()}T{time_str}:00"

try:
    # -----------------------------
    # 0. CLUB
    # -----------------------------

    club = track("Club", post("club", {
        "name": "Douro Padel",
    }))

    CLUB_ID = club["id"]
    print("Club ID:", CLUB_ID)

    # -----------------------------
    # 1. COACH USER + COACH
    # -----------------------------

    coach_user = track("User", post("user", {
        "name": "Bernardo Terroso",
        "username": "bernardo_terroso",
        "email": "bernardo@academy.pt",
        "phone": "+351900000000",
        "password": PASSWORD,
    }))
    
    coach_user2 = track("User", post("user", {
        "name": "Catarina Vilela",
        "username": "catarina_vilela",
        "email": "catarina_vilela@academy.pt",
        "phone": "+351900000010",
        "password": PASSWORD,
    }))

    USER_ID = coach_user["id"]
    USER2_ID = coach_user2["id"]
    print("User ID:", USER_ID)
    print("User ID:", USER2_ID)

    coach = track("Coach", post("coach", {
        "user": USER_ID
    }))
    
    coach2 = track("Coach", post("coach", {
        "user": USER2_ID
    }))

    COACH_ID = coach["id"]
    print("Coach ID:", COACH_ID)
    
    COACH2_ID = coach2["id"]
    print("Coach ID:", COACH2_ID)
    
    # -----------------------------
    # 2. COACH LEVELS
    # -----------------------------

    levels = {}

    levels["beginner"] = track("CoachLevel", post("coachlevel", {
        "coach": COACH_ID,
        "label": "Beginner",
        "code": "B",
    }))

    levels["intermediate"] = track("CoachLevel", post("coachlevel", {
        "coach": COACH_ID,
        "label": "Intermediate",
        "code": "I",
    }))

    levels["advanced"] = track("CoachLevel", post("coachlevel", {
        "coach": COACH_ID,
        "label": "Advanced",
        "code": "A",
    }))

    print("Coach levels created:", list(levels.keys()))

    # -----------------------------
    # 3. PLAYERS
    # -----------------------------

    players_data = [
        ("Pedro Pacheco", "pedropacheco@gmail.com", "+351918966340"),
        ("Tomás Pacheco", "tomaspacheco@gmail.com", "+34623456789"),
        ("Bernardo Castro", "bernardoc@gmail.com", None),
        ("Dudas BF", "dudasbf@gmail.com", "+351911111111"),
        ("Talinho Garrett", "talinho@gmail.com", None),
        ("António Neto", "antonioneto@gmail.com", "+351912222222"),
        ("Diogo Malafaya", "diogom@gmail.com", None),
        ("João Magalhães", "joaom@gmail.com", None),
    ]

    player_ids = []

    for name, email, phone in players_data:
        user =  track("User", post("user", {
            "name": name,
            "email": email,
            "phone": phone,
            "password": PASSWORD,
            "status": "active" if email in ["tomaspacheco@gmail.com", "dudasbf@gmail.com", "diogom@gmail.com"] else None,
            "username": email.split("@")[0],
        }))
        player = track("Player", post("player", {
            "user": user["id"],
            "coach": COACH_ID
        }))
        player_ids.append(player["id"])

    print("Players created:", len(player_ids))

    # -----------------------------
    # 4. LESSONS
    # -----------------------------

    start = today() - timedelta(days=2)
    lessons = {}

    # 4.1 Academy – 1 month
    lessons["academy_1_month"] = track("Lesson", post("lesson", {
        "title": "Academia Principiantes",
        "description": "Academia para iniciantes",
        "type": "academy",
        "status": "active",
        "color": "#0ea5e9",
        "max_players": 4,
        "default_level": levels["beginner"]["id"],
        "club": CLUB_ID,
        "coach": COACH_ID,
        "start_datetime": dt(start, "09:00"),
        "end_datetime": dt(start, "10:30"),
        "is_recurring": True,
        "recurrence_rule": json.dumps({
            "frequency": "weekly",
            "daysOfWeek": [1, 3],
        }),
        "recurrence_end": (start + relativedelta(months=1)).isoformat(),
        "player_ids": player_ids[:4],
    }))

    # 4.2 Academy – 2 weeks
    lessons["academy_2_weeks"] = track("Lesson", post("lesson", {
        "title": "Academia Intermédios",
        "description": "Grupo intermédio",
        "type": "academy",
        "status": "active",
        "color": "#8b5cf6",
        "max_players": 6,
        "default_level": levels["intermediate"]["id"],
        "club": CLUB_ID,
        "coach": COACH_ID,
        "start_datetime": dt(start + timedelta(days=1), "17:00"),
        "end_datetime": dt(start + timedelta(days=1), "18:30"),
        "is_recurring": True,
        "recurrence_rule": json.dumps({
            "frequency": "weekly",
            "daysOfWeek": [2, 4],
        }),
        "recurrence_end": (start + relativedelta(weeks=2)).isoformat(),
        "player_ids": [player_ids[1], player_ids[4], player_ids[5], player_ids[6]],
    }))

    # 4.3 Academy – 3 months
    lessons["academy_3_months"] = track("Lesson", post("lesson", {
        "title": "Academia Avançados",
        "description": "Treino avançado",
        "type": "academy",
        "status": "active",
        "color": "#22c55e",
        "max_players": 6,
        "default_level": levels["advanced"]["id"],
        "club": CLUB_ID,
        "coach": COACH_ID,
        "start_datetime": dt(start + timedelta(days=2), "19:00"),
        "end_datetime": dt(start + timedelta(days=2), "20:30"),
        "is_recurring": True,
        "recurrence_rule": json.dumps({
            "frequency": "weekly",
            "daysOfWeek": [1],
        }),
        "recurrence_end": (start + relativedelta(months=3)).isoformat(),
        "player_ids": [player_ids[0], player_ids[2], player_ids[3], player_ids[7]],
    }))

    # 4.4 Private – no recurrence
    lessons["private_single"] = track("Lesson", post("lesson", {
        "title": "Aula Privada António",
        "description": "Sessão individual",
        "type": "private",
        "status": "active",
        "color": "#ec4899",
        "max_players": 1,
        "default_level": levels["advanced"]["id"],
        "club": CLUB_ID,
        "coach": COACH_ID,
        "start_datetime": dt(start + timedelta(days=5), "09:00"),
        "end_datetime": dt(start + timedelta(days=5), "10:00"),
        "is_recurring": False,
        "player_ids": [player_ids[5]],
    }))

    print("Lessons created:", list(lessons.keys()))

    # -----------------------------
    # 5. CALENDAR BLOCKS
    # -----------------------------

    # Recurring block
    track("CalendarBlock", post("calendarblock", {
        "user": USER_ID,
        "title": "Almoço",
        "type": "break",
        "start_datetime": dt(today(), "13:00"),
        "end_datetime": dt(today(), "14:00"),
        "is_recurring": True,
        "recurrence_rule": json.dumps({
            "frequency": "weekly",
            "daysOfWeek": [1, 2, 3, 4, 5],
        }),
        "recurrence_end": (today() + relativedelta(months=1)).isoformat(),
    }))

    # One-off block
    track("CalendarBlock", post("calendarblock", {
        "user": USER_ID,
        "title": "Consulta médico",
        "type": "personal",
        "start_datetime": dt(today() + timedelta(days=4), "08:00"),
        "end_datetime": dt(today() + timedelta(days=4), "10:00"),
        "is_recurring": False,
    }))
    
    track("AssociationCoachClub", post("association_coachclub", {
        "coach": COACH_ID,
        "club": CLUB_ID,
    }))

    print("\n✅ FRONTEND DEMO DATA SEEDED SUCCESSFULLY")
    
except Exception as e:
    cleanup()
    print("\n❌ SEED FAILED — rollback completed")
    raise