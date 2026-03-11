from padel_app.tools.calendar_tools import expand_occurrences
from padel_app.models import (
    Lesson,
    LessonInstance,
    CalendarBlock,
    Association_CoachLesson,
    Association_PlayerLesson,
    Association_PlayerLessonInstance,
    Presence,
)
from padel_app.serializers.calendar_event import serialize_calendar_event


# ----------------------------
# Coach
# ----------------------------
def load_lessons_for_coach(coach_id, range_start, range_end):
    return (
        Lesson.query
        .join(Lesson.coaches_relations)
        .filter(
            Association_CoachLesson.coach_id == coach_id,
            Lesson.start_datetime <= range_end,
            Lesson.status == "active",
            (
                Lesson.recurrence_rule.is_(None)
                | (Lesson.recurrence_end.is_(None))
                | (Lesson.recurrence_end >= range_start.date())
            ),
        )
        .all()
    )


def load_lesson_instances_for_coach(coach_id, range_start, range_end):
    instances = (
        LessonInstance.query
        .join(Lesson)
        .filter(
            LessonInstance.start_datetime >= range_start,
            LessonInstance.start_datetime <= range_end,
        )
        .all()
    )

    indexed = {}
    for instance in instances:
        instance_coaches = (
            [rel.coach_id for rel in instance.coaches_relations]
            if instance.coaches_relations
            else [rel.coach_id for rel in instance.lesson.coaches_relations]
        )

        if coach_id not in instance_coaches:
            continue

        indexed[(instance.lesson_id, instance.original_lesson_occurence_date)] = instance

    return indexed


# ----------------------------
# Player
# ----------------------------
def load_lessons_for_player(player_id, range_start, range_end, *, only_active: bool = True):
    """
    Return base Lesson objects that the player is associated with (recurring templates).

    This uses the player<->lesson association (Association_PlayerLesson).
    """
    q = (
        Lesson.query
        .join(Lesson.players_relations)  # expects Lesson.players_relations relationship
        .filter(
            Association_PlayerLesson.player_id == player_id,
            Lesson.start_datetime <= range_end,
        )
    )

    if only_active:
        q = q.filter(Lesson.status == "active")

    # Keep recurrence filtering consistent with coach
    q = q.filter(
        (Lesson.recurrence_rule.is_(None))
        | (Lesson.recurrence_end.is_(None))
        | (Lesson.recurrence_end >= range_start.date())
    )

    return q.all()


def load_lesson_instances_for_player(
    player_id,
    range_start,
    range_end,
    *,
    include_invited: bool = True,
    include_confirmed_only: bool = False,
):
    """
    Return a dict indexed by (lesson_id, original_lesson_occurence_date) -> LessonInstance
    for instances relevant to this player.

    Priority / source of truth:
      1) Presence rows (invited/confirmed etc)
      2) Association_PlayerLessonInstance (if you use it)
    """
    indexed = {}

    pres_q = (
        Presence.query
        .join(LessonInstance, Presence.lesson_instance_id == LessonInstance.id)
        .filter(
            Presence.player_id == player_id,
            LessonInstance.start_datetime >= range_start,
            LessonInstance.start_datetime <= range_end,
        )
    )

    if include_confirmed_only:
        pres_q = pres_q.filter(Presence.confirmed == True)  # noqa: E712
    elif not include_invited:
        pres_q = pres_q.filter(Presence.invited == False)  # noqa: E712

    presences = pres_q.all()

    for p in presences:
        instance = p.lesson_instance
        if not instance:
            continue
        indexed[(instance.lesson_id, instance.original_lesson_occurence_date)] = instance

    rel_instances = (
        LessonInstance.query
        .join(Association_PlayerLessonInstance, Association_PlayerLessonInstance.lesson_instance_id == LessonInstance.id)
        .filter(
            Association_PlayerLessonInstance.player_id == player_id,
            LessonInstance.start_datetime >= range_start,
            LessonInstance.start_datetime <= range_end,
        )
        .all()
    )

    for instance in rel_instances:
        key = (instance.lesson_id, instance.original_lesson_occurence_date)
        indexed.setdefault(key, instance)

    return indexed


def build_lesson_events(lessons, instances_by_key, range_start, range_end):
    events = []

    rendered_instance_ids = set()
    for lesson in lessons:
        occurrences = expand_occurrences(
            lesson.start_datetime,
            lesson.recurrence_rule,
            lesson.recurrence_end,
            range_start,
            range_end,
        )

        for occ_start in occurrences:
            occ_date = occ_start.date()
            key = (lesson.id, occ_date)
            instance = instances_by_key.get(key)

            if instance:
                events.append(serialize_calendar_event(instance))
                rendered_instance_ids.add(instance.id)
            else:
                events.append(
                    serialize_calendar_event(
                        lesson,
                        override_id=f"lesson-{lesson.id}-{occ_date}",
                        override_date=occ_date.isoformat(),
                    )
                )

    for instance in instances_by_key.values():
        if instance.id not in rendered_instance_ids:
            events.append(serialize_calendar_event(instance))

    return events


def load_calendar_blocks_for_user(user_id, range_start, range_end):
    return (
        CalendarBlock.query
        .filter(
            CalendarBlock.user_id == user_id,
            CalendarBlock.start_datetime <= range_end,
            (
                CalendarBlock.recurrence_rule.is_(None)
                | (CalendarBlock.recurrence_end.is_(None))
                | (CalendarBlock.recurrence_end >= range_start.date())
            ),
        )
        .all()
    )


def build_block_events(blocks, range_start, range_end):
    events = []

    for block in blocks:
        occurrences = expand_occurrences(
            block.start_datetime,
            block.recurrence_rule,
            block.recurrence_end,
            range_start,
            range_end,
        )

        for occ_start in occurrences:
            occ_date = occ_start.date()
            events.append(
                serialize_calendar_event(
                    block,
                    override_id=f"block-{block.id}-{occ_start}",
                    override_date=occ_date.isoformat(),
                )
            )

    return events

def build_coach_calendar_events(coach_id, user_id, range_start, range_end, *, include_blocks: bool = True):
    lessons = load_lessons_for_coach(coach_id, range_start, range_end)
    instances_by_key = load_lesson_instances_for_coach(coach_id, range_start, range_end)
    lesson_events = build_lesson_events(lessons, instances_by_key, range_start, range_end)

    if not include_blocks:
        return lesson_events

    blocks = load_calendar_blocks_for_user(user_id, range_start, range_end)
    block_events = build_block_events(blocks, range_start, range_end)
    return lesson_events + block_events


def build_player_calendar_events(player_id, user_id, range_start, range_end, *, include_blocks: bool = True):
    lessons = load_lessons_for_player(player_id, range_start, range_end)
    instances_by_key = load_lesson_instances_for_player(player_id, range_start, range_end)
    lesson_events = build_lesson_events(lessons, instances_by_key, range_start, range_end)

    if not include_blocks:
        return lesson_events

    blocks = load_calendar_blocks_for_user(user_id, range_start, range_end)
    block_events = build_block_events(blocks, range_start, range_end)
    return lesson_events + block_events
