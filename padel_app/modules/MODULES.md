# Creating a Module

All API modules live in `padel_app/modules/`. This document covers everything you need to create one correctly.

---

## 1. File and Blueprint skeleton

Create a new file, e.g. `padel_app/modules/your_module_api.py`:

```python
from flask import Blueprint, abort, jsonify, request

from flask_jwt_extended import jwt_required

from padel_app.modules.frontend_api import current_coach, current_user

bp = Blueprint("your_module_api", __name__, url_prefix="/api/your_module")


@bp.get("/")
@jwt_required()
def list_items():
    coach = current_coach()
    # ... fetch and return data
    return jsonify([])


@bp.post("/")
@jwt_required()
def create_item():
    data = request.get_json() or {}
    # ... create and return item
    return jsonify({"id": 1}), 201
```

### URL prefix conventions

| Module type | Prefix |
|---|---|
| JSON API for the app | `/api/app/...` or `/api/<resource>` |
| Auth endpoints | `/api/auth` |
| Push / real-time | `/api/notifications` |
| Admin / editor | `/editor` |
| Session-based (non-API) | `/auth`, `/` |

Use `/api/app/<resource>` when the route belongs to the main frontend application, matching the pattern in `frontend_api.py`.

---

## 2. Register the blueprint

### `modules/__init__.py`

Import and register in `register_blueprints`:

```python
from . import your_module_api   # add this import

def register_blueprints(app):
    # ... existing registrations ...
    app.register_blueprint(your_module_api.bp)   # add this line
    return True
```

That's the only registration step — the app factory calls `modules.register_blueprints(app)` automatically.

---

## 3. Authentication

### Require a valid JWT on every route

```python
from flask_jwt_extended import jwt_required

@bp.get("/items")
@jwt_required()
def list_items():
    ...
```

`@jwt_required()` reads the `Authorization: Bearer <token>` header. Unauthenticated requests are rejected by the global JWT error handlers (401 / 422).

### Get the current user / coach / player

Use the helpers defined in `frontend_api.py`. They cache the DB lookup on the Flask `g` object so each request only hits the DB once per helper.

```python
from padel_app.modules.frontend_api import current_coach, current_player, current_user

@bp.get("/profile")
@jwt_required()
def profile():
    user  = current_user()   # → User model instance
    coach = current_coach()  # → Coach instance, or None
    # player = current_player()  # → Player instance, or None
    # club   = current_club()    # → Club instance (via coach)
    ...
```

`current_user()` aborts with 401 if no valid identity is in the token. `current_coach()` returns `None` if the user has no coach record — always guard before use:

```python
coach = current_coach()
if not coach:
    abort(403, "User is not a coach")
```

### JWT in query string (SSE only)

Server-Sent Events cannot send headers, so pass the token as a query parameter:

```python
@bp.route("/events")
@jwt_required(locations=["query_string"])
def events():
    ...
```

### Blanket auth for all routes in a blueprint

Use `@bp.before_request` when every route needs the same check:

```python
@bp.before_request
@jwt_required()
def require_auth():
    pass
```

Or use a custom decorator (see `auth_tools.admin_required` in `editor.py`).

---

## 4. Request parsing

### JSON body

```python
data = request.get_json() or {}   # always fall back to {}
name = data.get("name")
value = data.get("value", 0)      # with default
```

Never call `request.get_json()` without the `or {}` fallback — it returns `None` when the body is empty or the Content-Type header is missing.

### Query string parameters

```python
page     = request.args.get("page",     default=1,  type=int)
per_page = request.args.get("per_page", default=25, type=int)
query    = request.args.get("q", "")   # string, default empty
```

### URL path parameters

```python
@bp.get("/items/<int:item_id>")
def get_item(item_id):
    item = Item.query.get_or_404(item_id)
    ...
```

Use `<int:...>` for integer IDs; Flask coerces automatically and returns 404 on non-integer input.

### File uploads

```python
@bp.post("/import")
@jwt_required()
def import_file():
    file = request.files.get("file")
    if not file:
        return {"error": "No file provided"}, 400
    data = file.read()
    ...
```

---

## 5. Error handling

### `abort()`

```python
from flask import abort

abort(400, "name is required")
abort(403, "User is not a coach")
abort(404, "Item not found")
abort(500, "Unexpected error")
```

`abort()` immediately raises an HTTP exception that Flask's error handlers convert to a JSON response.

### `get_or_404()`

```python
item = Item.query.get_or_404(item_id)   # aborts with 404 automatically
```

### Status code tuples

```python
return {"id": item.id}, 201   # 201 Created
return "",                204   # 204 No Content (no body)
return {"error": "..."}, 400   # 400 Bad Request
```

Flask auto-converts plain dicts to JSON, so `jsonify()` is optional for dict responses.

### DB rollback on exception

```python
try:
    result = some_service(data)
except Exception:
    from padel_app.sql_db import db
    db.session.rollback()
    abort(500, "Operation failed")
```

Always rollback before aborting or re-raising when a DB write may be half-complete.

---

## 6. Responses

### Standard JSON

```python
return jsonify({"id": item.id, "name": item.name})
return jsonify([serialize_item(i) for i in items])
```

### Plain dict (shorthand)

```python
return {"id": item.id}, 201
```

### Using serializers

Serializer functions live in `padel_app/serializers/`. Use them to keep route handlers thin:

```python
from padel_app.serializers.lesson import serialize_lesson

@bp.get("/lessons")
@jwt_required()
def list_lessons():
    lessons = get_lessons_service(current_coach())
    return jsonify([serialize_lesson(l) for l in lessons])
```

### Server-Sent Events (streaming)

```python
import json
from flask import Response
from padel_app.sse import subscribe, unsubscribe

@bp.route("/stream")
@jwt_required(locations=["query_string"])
def stream():
    def generate():
        q = subscribe()
        try:
            while True:
                event = q.get()
                yield f"data: {json.dumps(event)}\n\n"
        except GeneratorExit:
            unsubscribe(q)

    return Response(generate(), mimetype="text/event-stream")
```

---

## 7. Service layer

Business logic belongs in `padel_app/services/`, not in route handlers. Route handlers should only:

1. Parse the request
2. Identify the current user/coach
3. Call a service function
4. Serialize and return the result

```python
# modules/your_module_api.py
from padel_app.services.your_service import create_item_service, get_items_service

@bp.post("/items")
@jwt_required()
def create_item():
    data  = request.get_json() or {}
    coach = current_coach()
    if not coach:
        abort(403, "User is not a coach")
    item = create_item_service(data, coach)   # all logic here
    return jsonify({"id": item.id}), 201

@bp.get("/items")
@jwt_required()
def list_items():
    coach = current_coach()
    if not coach:
        abort(403)
    items = get_items_service(coach)
    return jsonify([serialize_item(i) for i in items])
```

---

## 8. Complete example

```python
# padel_app/modules/exercise_api.py

from flask import Blueprint, abort, jsonify, request
from flask_jwt_extended import jwt_required

from padel_app.modules.frontend_api import current_coach
from padel_app.services.exercise_service import (
    create_exercise_service,
    delete_exercise_service,
    list_exercises_service,
    update_exercise_service,
)
from padel_app.serializers.exercise import serialize_exercise

bp = Blueprint("exercise_api", __name__, url_prefix="/api/app/exercises")


@bp.get("/")
@jwt_required()
def list_exercises():
    coach = current_coach()
    if not coach:
        abort(403, "User is not a coach")
    exercises = list_exercises_service(coach)
    return jsonify([serialize_exercise(e) for e in exercises])


@bp.post("/")
@jwt_required()
def create_exercise():
    data  = request.get_json() or {}
    coach = current_coach()
    if not coach:
        abort(403, "User is not a coach")
    exercise = create_exercise_service(data, coach)
    return jsonify(serialize_exercise(exercise)), 201


@bp.put("/<int:exercise_id>")
@jwt_required()
def update_exercise(exercise_id):
    data  = request.get_json() or {}
    coach = current_coach()
    if not coach:
        abort(403)
    exercise = update_exercise_service(exercise_id, data, coach)
    return jsonify(serialize_exercise(exercise))


@bp.delete("/<int:exercise_id>")
@jwt_required()
def delete_exercise(exercise_id):
    coach = current_coach()
    if not coach:
        abort(403)
    delete_exercise_service(exercise_id, coach)
    return "", 204
```

Then register it:

```python
# padel_app/modules/__init__.py
from . import exercise_api

def register_blueprints(app):
    ...
    app.register_blueprint(exercise_api.bp)
    return True
```

---

## 9. Checklist

- [ ] `bp = Blueprint("name", __name__, url_prefix="/api/...")`
- [ ] URL prefix follows the `/api/app/<resource>` convention for app APIs
- [ ] Every route that requires login has `@jwt_required()`
- [ ] `current_coach()` / `current_user()` used instead of calling `get_jwt_identity()` directly
- [ ] `current_coach()` result is guarded (`if not coach: abort(403)`) before use
- [ ] `request.get_json() or {}` used for all JSON body parsing
- [ ] `request.args.get("key", default=..., type=...)` for query params
- [ ] `get_or_404()` used for single-object lookups by ID
- [ ] All business logic delegated to `padel_app/services/`
- [ ] Responses use serializer functions from `padel_app/serializers/`
- [ ] Create/update routes return the created/updated resource (not just `{"ok": True}`)
- [ ] Delete routes return `"", 204`
- [ ] `db.session.rollback()` called before aborting on DB exceptions
- [ ] Blueprint imported and registered in `modules/__init__.py`
