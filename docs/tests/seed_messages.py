"""Dev-only conversation/message seeder.

PAD-92: this script used to POST anonymously to `/api/delete/<model>/<id>`
(rejected with 401 since PAD-88) and to the JWT-guarded `/api/app/conversation`
and `/api/app/message` routes without a token. It now authenticates as an
administrator and writes the rows through the admin CRUD API.

Going through the admin CRUD API rather than `/api/app/*` is deliberate: those
routes derive the sender and the timestamp from the calling JWT and "now", but a
demo transcript needs backdated messages attributed to several different people.

Export SEED_ADMIN_USERNAME / SEED_ADMIN_PASSWORD before running.
"""
from datetime import datetime

from _seed_auth import admin_session, create as api_create, delete as api_delete

SESSION = admin_session()

created_objects = []

def track(model, obj):
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

def iso(ts):
    return datetime.fromisoformat(ts).isoformat()

def participant_key(participant_ids):
    """Mirrors Conversation.build_participant_key (RULES.md #17)."""
    return ",".join(map(str, sorted(set(participant_ids))))

try:
    # -------------------------------------------------
    # USERS (must match previous seed order)
    # -------------------------------------------------

    COACH_USER_ID = 2  # Bernardo Terroso

    users = {
        'coach': COACH_USER_ID, # Bernardo Terroso
        "coach-2": 3,           # Catarina Vilela
        "player-2": 4,          # Tomás Pacheco
        "player-3": 5,          # Bernardo Castro
        "player-4": 6,          # Dudas BF
        "player-5": 7,          # Talinho Garrett
    }

    # -------------------------------------------------
    # CONVERSATIONS + MESSAGES
    # -------------------------------------------------

    conversations = [
        {
            "participants": ['coach', "coach-2"],
            "messages": [
                (COACH_USER_ID, "Hi Pedro, how are you? I’m texting to confirm tomorrow’s lesson.", "2024-01-15T09:00:00"),
                (users["coach-2"], "Hi! Yes, everything is confirmed. At 18:00 as usual?", "2024-01-15T09:15:00"),
                (COACH_USER_ID, "Exactly, 18:00 on court 3. Bring the new racket if you want to try it.", "2024-01-15T09:20:00"),
                (users["coach-2"], "Perfect, see you tomorrow then! I’ll arrive a bit earlier so we can warm up properly and review what we worked on last week.", "2024-01-15T10:30:00"),
            ],
        },
        {
            "participants": ['coach', "player-2"],
            "messages": [
                (users["player-2"], "Hi coach! I wanted to ask if there’s availability for an extra lesson this week.", "2024-01-14T14:00:00"),
                (COACH_USER_ID, "Hi Tomás! Let me check my schedule. I have a slot on Thursday at 17:00, does that work for you?", "2024-01-14T14:30:00"),
                (users["player-2"], "Perfect! Thursday works great for me. Shall we work on the backhand?", "2024-01-14T14:45:00"),
                (COACH_USER_ID, "Of course, we’ll focus on the backhand and the bandeja. Bring extra water!", "2024-01-14T15:00:00"),
                (users["player-2"], "Thanks for today’s lesson, I learned a lot about the backhand.", "2024-01-14T20:00:00"),
            ],
        },
        {
            "participants": ['coach', "player-3"],
            "messages": [
                (COACH_USER_ID, "Hi Bernardo, just letting you know that I won’t be able to give the lesson on Friday due to a personal commitment.", "2024-01-13T11:00:00"),
                (users["player-3"], "Understood, I’ll cancel Friday’s lesson.", "2024-01-13T11:30:00"),
            ],
        },
        {
            "participants": ['coach', "player-4"],
            "messages": [
                (users["player-4"], "Can we move Tuesday’s lesson to Wednesday?", "2024-01-12T16:00:00"),
            ],
        },
        {
            "participants": ['coach', "player-5"],
            "messages": [
                (COACH_USER_ID, "Talinho, remember to bring the new outfit for the group photo.", "2024-01-10T09:00:00"),
                (users["player-5"], "Perfect, thanks!", "2024-01-10T09:15:00"),
            ],
        },
    ]

    for conv in conversations:
        # -------------------------
        # Create conversation
        # -------------------------
        participant_ids = [users[p] for p in conv["participants"]]

        conversation = track("Conversation", post("conversation", {
            "is_group": False,
            "participant_ids": participant_ids,
            "creator_id": COACH_USER_ID,
            "participant_key": participant_key(participant_ids),
        }))

        CONV_ID = conversation["id"]

        # -------------------------
        # Create messages
        # -------------------------
        for sender_id, text, timestamp in conv["messages"]:
            track("Message", post("message", {
                "conversation": CONV_ID,
                "sender": sender_id,
                "text": text,
                "sent_at": iso(timestamp)
            }))

    print("\n✅ CONVERSATIONS & MESSAGES SEEDED SUCCESSFULLY")

except Exception:
    cleanup()
    print("\n❌ MESSAGE SEED FAILED — rollback completed")
    raise
