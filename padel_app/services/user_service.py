from padel_app.models import User
from padel_app.tools.request_adapter import JsonRequestAdapter


def create_user_service(data):
    user = User()
    form = user.get_create_form()

    fake_request = JsonRequestAdapter(data, form)
    values = form.set_values(fake_request)

    user.update_with_dict(values)
    user.create()
    return user


def edit_user_service(user_id, data):
    user = User.query.get_or_404(user_id)

    form = user.get_edit_form()
    fake_request = JsonRequestAdapter(data, form)
    values = form.set_values(fake_request)

    user.update_with_dict(values)
    user.save()
    return user


def activate_user_service(user_id, data):
    user = User.query.get_or_404(user_id)

    data['status'] = 'active'

    form = user.get_edit_form()
    fake_request = JsonRequestAdapter(data, form)
    values = form.set_values(fake_request)

    user.update_with_dict(values)
    user.save()
    return user
