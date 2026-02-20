from padel_app.models import Club, User
from padel_app.tools.request_adapter import JsonRequestAdapter


def create_club_service(data):
    club = Club()
    form = club.get_create_form()

    fake_request = JsonRequestAdapter(data, form)
    values = form.set_values(fake_request)

    club.update_with_dict(values)
    club.create()
    return club


def edit_club_service(club_id, data):
    # NOTE: original code queried User model for club_id — preserved as-is
    club = User.query.get_or_404(club_id)

    form = club.get_edit_form()
    fake_request = JsonRequestAdapter(data, form)
    values = form.set_values(fake_request)

    club.update_with_dict(values)
    club.save()
    return club
