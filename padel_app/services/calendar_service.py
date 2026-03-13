import json
from datetime import timedelta, datetime as _datetime, date as _date, timezone

from padel_app.models import CalendarBlock
from padel_app.tools.request_adapter import JsonRequestAdapter
from padel_app.tools.calendar_tools import expand_occurrences


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _clone_block(src, *, user_id, **overrides):
    """Create and persist a new CalendarBlock copying src fields, applying overrides."""
    new = CalendarBlock()
    new.user_id = user_id
    new.type = overrides.get('type', src.type)
    new.title = overrides.get('title', src.title)
    new.description = overrides.get('description', src.description)
    new.start_datetime = overrides.get('start_datetime', src.start_datetime)
    new.end_datetime = overrides.get('end_datetime', src.end_datetime)
    new.is_recurring = overrides.get('is_recurring', src.is_recurring)
    new.recurrence_rule = overrides.get('recurrence_rule', src.recurrence_rule)
    new.recurrence_end = overrides.get('recurrence_end', src.recurrence_end)
    new.create()
    return new


def _next_occurrence_after(block, after_date):
    """Return the first occurrence datetime of block after after_date (exclusive)."""
    after_dt = _datetime.combine(after_date, _datetime.min.time()).replace(tzinfo=timezone.utc)
    end_dt = _datetime.combine(
        block.recurrence_end if block.recurrence_end else (after_date + timedelta(days=400)),
        _datetime.max.time(),
    ).replace(tzinfo=timezone.utc)
    occs = expand_occurrences(
        block.start_datetime, block.recurrence_rule, block.recurrence_end,
        after_dt + timedelta(seconds=1), end_dt,
    )
    return occs[0] if occs else None


def _split_block(block, occ_date):
    """
    Remove occ_date from recurring block by splitting:
      - Original series ends the day before occ_date.
      - A new copy of the series resumes from the next occurrence.
    """
    original_end = block.recurrence_end
    duration = block.end_datetime - block.start_datetime

    if occ_date <= block.start_datetime.date():
        # Deleting the very first occurrence – advance start to next
        next_occ = _next_occurrence_after(block, occ_date)
        if next_occ:
            block.start_datetime = _datetime.combine(next_occ.date(), block.start_datetime.time())
            block.end_datetime = block.start_datetime + duration
            block.save()
        else:
            block.delete()
        return

    block.recurrence_end = occ_date - timedelta(days=1)
    block.save()

    next_occ = _next_occurrence_after(block, occ_date)
    if next_occ:
        _clone_block(
            block,
            user_id=block.user_id,
            start_datetime=_datetime.combine(next_occ.date(), block.start_datetime.time()),
            end_datetime=_datetime.combine(next_occ.date(), block.start_datetime.time()) + duration,
            recurrence_end=original_end,
        )


def _build_payload(data):
    """Build a form-compatible payload dict from frontend add_event/edit_event data."""
    is_recurring = data.get("isRecurring", False)
    return {
        "type": data["type"],
        "title": data.get("title") or "",
        "description": data.get("description") or "",
        "start_datetime": f"{data['date']}T{data['startTime']}",
        "end_datetime": f"{data['date']}T{data['endTime']}",
        "is_recurring": "true" if is_recurring else "false",
        "recurrence_rule": json.dumps(data["recurrenceRule"]) if is_recurring else "",
        "recurrence_end": data.get("endDate") or "",
    }


# ---------------------------------------------------------------------------
# Admin form-based services (used by editor)
# ---------------------------------------------------------------------------

def create_calendar_block_service(data):
    block = CalendarBlock()
    form = block.get_create_form()
    fake_request = JsonRequestAdapter(data, form)
    values = form.set_values(fake_request)
    block.update_with_dict(values)
    block.create()
    return block


def edit_calendar_block_service(block_id, data):
    block = CalendarBlock.query.get_or_404(block_id)
    form = block.get_edit_form()
    fake_request = JsonRequestAdapter(data, form)
    values = form.set_values(fake_request)
    block.update_with_dict(values)
    block.save()
    return block


# ---------------------------------------------------------------------------
# App-facing services
# ---------------------------------------------------------------------------

def add_event_service(user_id, data):
    """Create a CalendarBlock for a user from frontend add_event data."""
    payload = {"user": user_id, **_build_payload(data)}
    block = CalendarBlock()
    form = block.get_create_form()
    fake_request = JsonRequestAdapter(payload, form)
    values = form.set_values(fake_request)
    block.update_with_dict(values)
    block.create()
    return block


def edit_event_service(block_id, user_id, data):
    """Edit a CalendarBlock owned by user_id from frontend edit_event data."""
    block = CalendarBlock.query.filter_by(id=block_id, user_id=user_id).first_or_404()
    form = block.get_create_form()
    fake_request = JsonRequestAdapter(_build_payload(data), form)
    values = form.set_values(fake_request)
    block.update_with_dict(values)
    block.save()
    return block


def reschedule_block_service(block_id, user_id, data):
    """
    Drag-and-drop reschedule.  data keys:
      occDate, newDate, newStartTime, newEndTime, scope ('single'|'future')
    """
    block = CalendarBlock.query.filter_by(id=block_id, user_id=user_id).first_or_404()

    occ_date = _date.fromisoformat(data['occDate'])
    new_start_dt = _datetime.strptime(f"{data['newDate']}T{data['newStartTime']}", "%Y-%m-%dT%H:%M")
    new_end_dt = _datetime.strptime(f"{data['newDate']}T{data['newEndTime']}", "%Y-%m-%dT%H:%M")
    scope = data.get('scope', 'single')

    if not block.is_recurring:
        block.start_datetime = new_start_dt
        block.end_datetime = new_end_dt
        block.save()
        return

    if scope == 'future':
        original_end = block.recurrence_end
        if occ_date > block.start_datetime.date():
            block.recurrence_end = occ_date - timedelta(days=1)
            block.save()
            _clone_block(block, user_id=user_id,
                         start_datetime=new_start_dt, end_datetime=new_end_dt,
                         recurrence_end=original_end)
        else:
            # Moving the very first occurrence – update in place
            block.start_datetime = new_start_dt
            block.end_datetime = new_end_dt
            block.save()

    elif scope == 'single':
        # Create one-off at new time, split original to skip this occurrence
        _clone_block(block, user_id=user_id,
                     start_datetime=new_start_dt, end_datetime=new_end_dt,
                     is_recurring=False, recurrence_rule=None, recurrence_end=None)
        _split_block(block, occ_date)


def remove_block_service(block_id, user_id, occ_date_str, scope):
    """
    Scope-aware delete.
      scope='single'  – removes only this occurrence (splits recurring series)
      scope='future'  – removes this and all following occurrences
    """
    block = CalendarBlock.query.filter_by(id=block_id, user_id=user_id).first_or_404()

    if not block.is_recurring or not occ_date_str:
        block.delete()
        return

    occ_date = _date.fromisoformat(occ_date_str)

    if scope == 'future':
        if occ_date <= block.start_datetime.date():
            block.delete()
        else:
            block.recurrence_end = occ_date - timedelta(days=1)
            block.save()

    elif scope == 'single':
        _split_block(block, occ_date)
