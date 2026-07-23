"""Canonical ordering of a coach's skill-level ladder.

A coach defines their own levels (see ``specs/levels/spec.md``). The ordering
convention across the whole codebase is:

    **lower ``display_order`` = HIGHER / stronger skill level**

``display_order`` is derived from the position of the row in the settings list
(``displayOrder: i + 1``), so the *first* level a coach lists is their strongest
and the last is their weakest.

Two things make the raw integer unsafe to compare directly:

* ``CoachLevel.display_order`` has a Python-side default of ``0`` and is
  nullable, so any write path that does not supply an order (single-level POST,
  spreadsheet/AI import, rows predating the column) produces a level that sorts
  ahead of *every* explicitly ordered level — i.e. is read as the coach's
  strongest level. That is PAD-70: a "5-" level created this way was treated as
  one level above a "4" vacancy.
* Values can be duplicated or sparse, so "the next distinct integer" is not the
  same thing as "the next level in the coach's ladder".

Consumers must therefore reason about a level's **position** in the ordered
ladder, not about its ``display_order`` value. This module is the single place
that defines that ordering.
"""


def is_unordered(display_order) -> bool:
    """True when a level carries no meaningful explicit order.

    ``None`` (nullable column) and ``0`` (the column default) both mean "nobody
    told us where this level belongs". Explicit orders are ``1..N``.
    """
    return display_order is None or display_order <= 0


def ladder_sort_key(level):
    """Sort key placing the strongest level first.

    Explicitly ordered levels come first by ascending ``display_order``;
    unordered levels fall to the bottom of the ladder. ``id`` breaks ties so the
    ladder is stable and deterministic.
    """
    order = level.display_order
    unordered = is_unordered(order)
    return (1 if unordered else 0, 0 if unordered else order, level.id or 0)


def sort_ladder(levels):
    """Return ``levels`` ordered strongest → weakest."""
    return sorted(levels, key=ladder_sort_key)


def get_level_ladder(coach_id: int) -> list:
    """The coach's levels ordered strongest → weakest."""
    from padel_app.models.coach_levels import CoachLevel

    return sort_ladder(CoachLevel.query.filter_by(coach_id=coach_id).all())


def ladder_index(ladder: list, level_id) -> int | None:
    """0-based position of ``level_id`` in ``ladder`` (0 = strongest), or None."""
    if level_id is None:
        return None
    for index, level in enumerate(ladder):
        if level.id == level_id:
            return index
    return None


def ladder_index_map(coach_id: int) -> dict:
    """``{level_id: position}`` for the coach's ladder (0 = strongest)."""
    return {level.id: index for index, level in enumerate(get_level_ladder(coach_id))}


def next_display_order(coach_id: int) -> int:
    """The order a newly created level should take: the bottom of the ladder.

    Appending is the only safe default — a level whose position the caller did
    not specify must never outrank the levels the coach explicitly ordered.
    """
    from padel_app.models.coach_levels import CoachLevel

    orders = [
        lv.display_order
        for lv in CoachLevel.query.filter_by(coach_id=coach_id).all()
        if not is_unordered(lv.display_order)
    ]
    return (max(orders) + 1) if orders else 1


def normalize_display_orders(coach_id: int) -> None:
    """Renumber a coach's levels to a contiguous ``1..N`` in ladder order.

    Called after any bulk write so that gaps, duplicates and unset orders can
    never reach the notification engine. Does not commit.
    """
    for index, level in enumerate(get_level_ladder(coach_id), start=1):
        if level.display_order != index:
            level.display_order = index
