from datetime import date, datetime
from decimal import Decimal

from flask import Blueprint, abort, jsonify, request
from flask_jwt_extended import get_jwt_identity, jwt_required, verify_jwt_in_request

from padel_app.models import MODELS, User
from padel_app.sql_db import db

bp = Blueprint("editor_api", __name__, url_prefix="/api/editor")


@bp.before_request
def require_superadmin():
    verify_jwt_in_request()
    user_id = int(get_jwt_identity())
    user = User.query.get_or_404(user_id)
    if not user.is_superadmin:
        abort(403, "Superadmin access required")


def serialize(instance):
    result = {}
    for col in instance.__table__.columns:
        val = getattr(instance, col.name)
        if isinstance(val, (datetime, date)):
            result[col.name] = val.isoformat()
        elif isinstance(val, Decimal):
            result[col.name] = float(val)
        else:
            result[col.name] = val
    return result


def get_instance_label(instance):
    try:
        name = getattr(instance, "name", None) or getattr(instance, "username", None)
        if name:
            return str(name)
        return str(instance.id)
    except Exception:
        return str(instance.id)


@bp.get("/models")
def list_models():
    result = []
    for key, cls in MODELS.items():
        try:
            empty = cls()
            searchable, columns = empty.display_all_info()
            result.append({
                "key": key,
                "title": getattr(cls, "page_title", key),
                "searchableColumn": searchable,
                "listColumns": columns,
            })
        except (NotImplementedError, Exception):
            result.append({
                "key": key,
                "title": getattr(cls, "page_title", key),
                "searchableColumn": None,
                "listColumns": [],
            })
    return jsonify(result)


@bp.get("/<model>/schema")
def model_schema(model):
    model_cls = MODELS.get(model.lower())
    if not model_cls:
        abort(404, f"Model '{model}' not found")
    try:
        form = model_cls().get_create_form()
        fields = [field.get_field_dict() for field in form.fields]
        return jsonify(fields)
    except NotImplementedError:
        return jsonify([])


@bp.get("/<model>/options")
def model_options(model):
    model_cls = MODELS.get(model.lower())
    if not model_cls:
        abort(404, f"Model '{model}' not found")
    instances = model_cls.query.all()
    return jsonify([{"id": i.id, "label": get_instance_label(i)} for i in instances])


@bp.get("/<model>")
def list_records(model):
    model_cls = MODELS.get(model.lower())
    if not model_cls:
        abort(404, f"Model '{model}' not found")

    page = request.args.get("page", 1, type=int)
    per_page = 50
    search = request.args.get("search", "").strip()

    try:
        empty = model_cls()
        searchable, _ = empty.display_all_info()
        searchable_field = searchable.get("field") if isinstance(searchable, dict) else searchable
    except (NotImplementedError, Exception):
        searchable_field = None

    query = model_cls.query.order_by(model_cls.id.asc())

    if search and searchable_field and hasattr(model_cls, searchable_field):
        col = getattr(model_cls, searchable_field)
        query = query.filter(col.ilike(f"%{search}%"))

    pagination = query.paginate(page=page, per_page=per_page, error_out=False)

    return jsonify({
        "items": [serialize(item) for item in pagination.items],
        "total": pagination.total,
        "pages": pagination.pages,
        "page": page,
    })


@bp.get("/<model>/<int:record_id>")
def get_record(model, record_id):
    model_cls = MODELS.get(model.lower())
    if not model_cls:
        abort(404, f"Model '{model}' not found")
    instance = model_cls.query.get_or_404(record_id)
    return jsonify(serialize(instance))


@bp.post("/<model>")
def create_record(model):
    model_cls = MODELS.get(model.lower())
    if not model_cls:
        abort(404, f"Model '{model}' not found")

    data = request.get_json() or {}
    values = data.get("values", {})

    if not values:
        abort(400, "No values provided")

    instance = model_cls()
    instance.update_with_dict(values)
    instance.create()
    return jsonify({"id": instance.id}), 201


@bp.patch("/<model>/<int:record_id>")
def update_record(model, record_id):
    model_cls = MODELS.get(model.lower())
    if not model_cls:
        abort(404, f"Model '{model}' not found")

    instance = model_cls.query.get_or_404(record_id)
    data = request.get_json() or {}
    values = data.get("values", {})

    if not values:
        abort(400, "No values provided")

    instance.update_with_dict(values)
    instance.save()
    return jsonify({"id": instance.id})


@bp.delete("/<model>/<int:record_id>")
def delete_record(model, record_id):
    model_cls = MODELS.get(model.lower())
    if not model_cls:
        abort(404, f"Model '{model}' not found")

    instance = model_cls.query.get_or_404(record_id)
    instance.delete()
    return jsonify({"success": True})
