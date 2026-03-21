from .token_blocklist import TokenBlocklist
from .backend_apps import Backend_App
from .clubs import Club
from .coach_levels import CoachLevel
from .coaches import Coach
from .lesson_instances import LessonInstance
from .lessons import Lesson
from .messages import Message
from .message_reaction import MessageReaction
from .push_subscriptions import PushSubscription
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
from .exercise import Exercise, ExerciseGroup
from .Association_CoachClub import Association_CoachClub
from .Association_CoachLesson import Association_CoachLesson
from .Association_CoachLessonInstance import Association_CoachLessonInstance
from .Association_CoachPlayer import Association_CoachPlayer
from .Association_PlayerClub import Association_PlayerClub
from .Association_PlayerLesson import Association_PlayerLesson
from .Association_PlayerLessonInstance import Association_PlayerLessonInstance
from .Association_CoachExercise import Association_CoachExercise
from .Association_CoachExerciseGroup import Association_CoachExerciseGroup
from .lesson_instance_training import LessonInstanceTraining
from .notification_config import NotificationConfig
from .notification_event import NotificationEvent
from .vacancy import Vacancy
from .waiting_list_entry import WaitingListEntry
from .standing_waiting_list_entry import StandingWaitingListEntry

MODELS = {
    "tokenblocklist": TokenBlocklist,
    "backend_app": Backend_App,
    "club": Club,
    "coachlevel": CoachLevel,
    "coach": Coach,
    "lessoninstance": LessonInstance,
    "lesson": Lesson,
    "lessage": Message,
    "messagereaction": MessageReaction,
    "pushsubscription": PushSubscription,
    "playerlevelhistory": PlayerLevelHistory,
    "player": Player,
    "user": User,
    "presence": Presence,
    "calendarblock": CalendarBlock,
    "conversation": Conversation,
    "coachplayernote": CoachPlayerNote,
    "evaluationcategory": EvaluationCategory,
    "evaluationentry": EvaluationEntry,
    "exercise": Exercise,
    "exercisegroup": ExerciseGroup,
    "conversation_participant": ConversationParticipant,
    "association_coachclub": Association_CoachClub,
    "association_coachlesson": Association_CoachLesson,
    "association_coachlessoninstance": Association_CoachLessonInstance,
    "association_coachplayer": Association_CoachPlayer,
    "association_playerclub": Association_PlayerClub,
    "association_playerlesson": Association_PlayerLesson,
    "association_playerlessoninstance": Association_PlayerLessonInstance,
    "association_coachexercise": Association_CoachExercise,
    "association_coachexercisegroup": Association_CoachExerciseGroup,
    "lesson_instance_training": LessonInstanceTraining,
    "notificationconfig": NotificationConfig,
    "notificationevent": NotificationEvent,
    "vacancy": Vacancy,
    "waitinglistentry": WaitingListEntry,
    "standingwaitinglistentry": StandingWaitingListEntry,
}
