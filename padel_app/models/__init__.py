from .backend_apps import Backend_App
from .clubs import Club
from .coach_levels import CoachLevel
from .coaches import Coach
from .lesson_instances import LessonInstance
from .lessons import Lesson
from .messages import Message
from .player_level_history import PlayerLevelHistory
from .players import Player
from .users import User
from .presences import Presence
from .calendar_blocks import CalendarBlock
from .conversations import Conversation
from .conversation_participants import ConversationParticipant
from .coach_player_note import CoachPlayerNote
from .evaluation_category import EvaluationCategory
from .evaluation_entry import EvaluationEntry
from .Association_CoachClub import Association_CoachClub
from .Association_CoachLesson import Association_CoachLesson
from .Association_CoachLessonInstance import Association_CoachLessonInstance
from .Association_CoachPlayer import Association_CoachPlayer
from .Association_PlayerClub import Association_PlayerClub
from .Association_PlayerLesson import Association_PlayerLesson
from .Association_PlayerLessonInstance import Association_PlayerLessonInstance

MODELS = {
    "backend_app": Backend_App,
    "club": Club,
    "coachlevel": CoachLevel,
    "coach": Coach,
    "lessoninstance": LessonInstance,
    "lesson": Lesson,
    "lessage": Message,
    "playerlevelhistory": PlayerLevelHistory,
    "player": Player,
    "user": User,
    "presence": Presence,
    "calendarblock": CalendarBlock,
    "conversation": Conversation,
    "coachplayernote": CoachPlayerNote,
    "evaluationcategory": EvaluationCategory,
    "evaluationentry": EvaluationEntry,
    "conversation_participant": ConversationParticipant,
    "association_coachclub": Association_CoachClub,
    "association_coachlesson": Association_CoachLesson,
    "association_coachlessoninstance": Association_CoachLessonInstance,
    "association_coachplayer": Association_CoachPlayer,
    "association_playerclub": Association_PlayerClub,
    "association_playerlesson": Association_PlayerLesson,
    "association_playerlessoninstance": Association_PlayerLessonInstance,
}
