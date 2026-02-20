from padel_app.models import CalendarBlock
from padel_app.tools.request_adapter import JsonRequestAdapter


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
