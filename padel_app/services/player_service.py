from padel_app.models import (
    Player,
    User,
    Association_CoachPlayer,
    PlayerLevelHistory,
)
from sqlalchemy.orm import joinedload
from padel_app.tools.request_adapter import JsonRequestAdapter


# ---------------------------------------------------------------------------
# Moved from helpers/player_services.py
# ---------------------------------------------------------------------------

def create_player_helper(data):
    player = Player()
    player_form = player.get_create_form()

    user = User()
    user_form = user.get_create_form()

    user_fake_request = JsonRequestAdapter(data['user'], user_form)
    user_values = user_form.set_values(user_fake_request)

    user.update_with_dict(user_values)
    user.create()

    player_data = {'user': user.id}

    player_fake_request = JsonRequestAdapter(player_data, player_form)
    player_values = player_form.set_values(player_fake_request)

    player.update_with_dict(player_values)
    player.create()

    if data.get("coach"):
        rel_data = {
            'coach': data.get("coach"),
            'player': player.id,
            'level': data.get('level', None),
            'side': data.get('side', None),
            'notes': data.get('notes', None),
        }
        rel = Association_CoachPlayer()

        rel_form = rel.get_create_form()

        rel_fake_request = JsonRequestAdapter(rel_data, rel_form)
        rel_values = rel_form.set_values(rel_fake_request)

        rel.update_with_dict(rel_values)
        rel.create()

    if data.get("coach") and data['level']:
        PlayerLevelHistory(
            coach_id=data["coach"],
            player_id=player.id,
            level_id=data['level']
        ).create()

    return player.coach_player_info(data["coach"])


def edit_player_helper(player, rel, data):
    user_form = player.user.get_edit_form()
    user_fake_request = JsonRequestAdapter(data['user'], user_form)
    user_values = user_form.set_values(user_fake_request)

    player.user.update_with_dict(user_values)
    player.user.save()

    rel_form = rel.get_edit_form()
    rel_fake_request = JsonRequestAdapter(data['relation'], rel_form)
    rel_values = rel_form.set_values(rel_fake_request)

    rel.update_with_dict(rel_values)
    rel.save()

    return player.coach_player_info(data["coach"])


# ---------------------------------------------------------------------------
# Route-level service functions (extracted from frontend_api.py)
# ---------------------------------------------------------------------------

def create_player_service(data):
    """Creates a Player record via form, and optionally links it to a coach."""
    player = Player()
    form = player.get_create_form()

    fake_request = JsonRequestAdapter(data, form)
    values = form.set_values(fake_request)

    player.update_with_dict(values)
    player.create()

    if data.get("coach"):
        Association_CoachPlayer(
            coach_id=data["coach"],
            player_id=player.id,
        ).create()

    return player


def get_players_list(coach, club):
    """Returns the appropriate player list based on the caller's role."""
    if coach:
        return coach.players
    elif club:
        return club.players
    else:
        return Player.query.all()


def _serialize_coach_player_relation(rel):
    player = rel.player
    user = player.user if player else None
    return {
        "id": f"p-{rel.player_id}_c-{rel.coach_id}",
        "coachId": rel.coach_id,
        "playerId": rel.player_id,
        "levelId": rel.level_id,
        "notes": rel.notes,
        "name": user.name if user else None,
        "email": user.email if user else None,
        "phone": user.phone if user else None,
        "username": user.username if user else None,
        "side": rel.side,
        "userId": player.user_id if player else None,
        "isActive": user.status == "active" if user else False,
    }


def get_coach_players_list(coach):
    relations = (
        Association_CoachPlayer.query.options(
            joinedload(Association_CoachPlayer.player).joinedload(Player.user)
        )
        .filter_by(coach_id=coach.id)
        .order_by(Association_CoachPlayer.id.desc())
        .all()
    )
    return [_serialize_coach_player_relation(rel) for rel in relations]


def get_coach_players_paginated(coach, page=1, per_page=25, search=None):
    query = (
        Association_CoachPlayer.query.options(
            joinedload(Association_CoachPlayer.player).joinedload(Player.user)
        )
        .filter_by(coach_id=coach.id)
    )
    if search:
        query = query.join(Association_CoachPlayer.player).join(Player.user).filter(
            User.name.ilike(f"%{search}%")
        )
    query = query.order_by(Association_CoachPlayer.id.desc())
    pagination = query.paginate(page=page, per_page=per_page, error_out=False)
    return {
        "items": [_serialize_coach_player_relation(rel) for rel in pagination.items],
        "pagination": {
            "page": pagination.page,
            "perPage": pagination.per_page,
            "total": pagination.total,
            "pages": pagination.pages,
            "hasNext": pagination.has_next,
            "hasPrev": pagination.has_prev,
        },
    }


def get_player_profile(coach, player_id):
    """Returns evaluation profile data for a player under a coach."""
    coach_player = (
        Association_CoachPlayer.query
        .filter_by(coach_id=coach.id, player_id=player_id)
        .first_or_404()
    )

    evaluations = [
        {
            "categoryId": entry.category_id,
            "categoryName": entry.category.name,
            "score": entry.score,
            "scaleMin": entry.category.scale_min,
            "scaleMax": entry.category.scale_max,
            "evaluatedAt": entry.evaluated_at.isoformat(),
        }
        for entry in coach_player.current_evaluations
    ]

    return {
        "playerId": str(player_id),
        "evaluations": evaluations,
        "strengths": [{"id": n.id, "text": n.text} for n in coach_player.strengths],
        "weaknesses": [{"id": n.id, "text": n.text} for n in coach_player.weaknesses],
    }


def add_player_service(data):
    """Builds the full player creation payload and delegates to create_player_helper."""
    payload = {
        'coach': int(data['coachId']) if data['coachId'] else None,
        'level': int(data['levelId']) if data.get('levelId', None) else None,
        'side': data.get('side', None),
        'notes': data.get('notes', None),
        'user': {
            'name': data.get('name', None),
            'username': data.get('username', None),
            'email': data.get('email', None),
            'phone': data.get('phone', None),
        },
    }
    return create_player_helper(payload)


def edit_player_service(data):
    """Computes changed fields and delegates to edit_player_helper."""
    updates = data['updates']
    player_info = data['player']

    changes = {k: v for k, v in updates.items() if v != player_info.get(k)}

    payload = {
        'coach': player_info['coachId'],
        'relation': {
            'level': int(changes['levelId']) if changes.get('levelId', None) else None,
            'side': changes.get('side', None),
            'notes': changes.get('notes', None),
        },
        'user': {
            'name': changes.get('name', None),
            'username': changes.get('username', None),
            'email': changes.get('email', None),
            'phone': changes.get('phone', None),
        },
    }

    player = Player.query.get_or_404(player_info['playerId'])
    rel = Association_CoachPlayer.query.filter_by(
        coach_id=player_info['coachId'],
        player_id=player_info["playerId"],
    ).first_or_404()

    return edit_player_helper(player, rel, payload)


def remove_player_service(data):
    """Removes or deletes a player depending on coach count and account status.

    - Multiple coaches: only remove the coach-player relationship.
    - Single coach + active player: delete the player record (cascades associations).
    - Single coach + inactive player: delete both player and user records.
    """
    coach_id = data.get("coachId", None)
    player_id = data.get("playerId", None)

    player = Player.query.get_or_404(player_id)
    user = player.user

    rel = Association_CoachPlayer.query.filter_by(
        coach_id=coach_id,
        player_id=player_id,
    ).first_or_404()

    coach_count = Association_CoachPlayer.query.filter_by(player_id=player_id).count()

    if coach_count > 1:
        rel.delete()
        return {"status": "Removed coach-player relationship"}, 200
    elif user.status == "active":
        player.delete()
        return {"status": "Deleted active player"}, 200
    else:
        player.delete()
        user.delete()
        return {"status": "Deleted inactive player and user"}, 200
