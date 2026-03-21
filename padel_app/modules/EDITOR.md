# Admin Editor

The editor is a fully generic admin interface at `/editor`. Adding support for a new model requires only two classmethods on the model — no new routes or templates needed.

---

## How it works

The editor reads the `MODELS` dict from `padel_app/models/__init__.py` to resolve model classes from URL segments:

```
/editor/display/<model>          → list view
/editor/display/<model>/<id>     → detail / edit view
/editor/create/<model>           → create form
```

The key in `MODELS` must be the **lowercase class name, no underscores** (e.g. `"coachlevel"` for `CoachLevel`). It is used for URL routing.

All routes are protected by `@auth_tools.admin_required` — the logged-in user must have `is_admin = True`.

---

## The two required classmethods

### `display_all_info()`

Controls the list view: which columns to show in the table, and which column supports client-side search.

```python
@classmethod
def display_all_info(cls):
    searchable = {"field": "label", "label": "Level"}   # one searchable column
    columns = [
        {"field": "coach", "label": "Coach"},
        {"field": "label", "label": "Level"},
        {"field": "display_order", "label": "Order"},
    ]
    return searchable, columns
```

- `searchable` — the single field used for the real-time search box. `field` must be an attribute on the model instance (can be a relationship name if it has a `__str__`/`name` property). `label` is the placeholder text.
- `columns` — the table headers. Each `field` is accessed as `getattr(instance, field)` for display. Relationship names work as long as the related object has a usable string representation.

### `get_create_form()`

Controls both the create and edit forms. It must declare a `Field` for **every column or relationship the admin should be able to set**. Fields omitted here are invisible in the admin and ignored by `update_with_dict`.

See [MODELS.md](../models/MODELS.md) — Section 3 documents `get_create_form()` exhaustively, including all `Field` types, `Block`/`Tab` structure, and a complete example.

---

## List view behaviour

- 100 rows per page, paginated with Previous / Next.
- Clicking a row navigates to the detail page.
- Column headers are sortable (client-side).
- The search box filters visible rows in real time against `searchable.field`.
- The **Actions** dropdown exposes:
  - **Delete** — shows a trash icon on each row (calls `/api/delete/<model>/<id>`).
  - **Download CSV** — exports the current page.
  - **Upload CSV** — bulk-imports from a CSV file.

---

## Create flow

1. GET `/editor/create/<model>` — renders an empty form from `get_create_form()`.
2. User fills fields and clicks **Save**.
3. POST `/editor/create/<model>` — the editor calls `form.set_values(request)` to extract typed values, then `instance.update_with_dict(values)` and `instance.create()`.
4. Redirects to the list page on success.

---

## Edit flow

1. GET `/editor/display/<model>/<id>` — renders the form pre-filled via `get_edit_form()` (which delegates to `get_create_form()` and populates values). All inputs are `readonly`.
2. User clicks **Edit** — JavaScript removes the `readonly` attributes.
3. User modifies fields and clicks **Save** — the form is submitted via a hidden `<iframe>` to `POST /api/edit/<model>/<id>`.
4. The API calls `form.set_values(request)` → `obj.update_with_dict(values)` → `obj.save()`.
5. JavaScript re-adds `readonly` to return the page to read mode.

---

## API endpoints that support the editor

These live in `modules/api.py` (prefix `/api`):

| Endpoint | Method | Purpose |
|---|---|---|
| `/api/create/<model>` | POST | Create a new instance |
| `/api/edit/<model>/<id>` | POST | Update an existing instance |
| `/api/delete/<model>/<id>` | GET/POST | Delete an instance |
| `/api/query/<model>` | GET/POST | Return all instances as JSON |
| `/api/remove_relationship` | POST | Remove one item from a many-to-many |
| `/api/modal_create_page/<model>` | GET/POST | Inline creation modal (used by relationship fields) |
| `/api/download_csv/<model>` | GET | Export model as CSV |
| `/api/upload_csv_to_db/<model>` | POST | Bulk-import CSV |
| `/api/image/<int:image_id>` | GET | Redirect to the image's GCS URL |

---

## Field types and how they are saved

When the form is submitted, `Field.set_value(request)` extracts and converts each value before it is passed to `update_with_dict`. The key conversions:

| Type | What `set_value` does |
|---|---|
| `Text`, `Select` | `request.form[name]`, or `None` if empty |
| `Integer`, `Float` | Parses from string, or `None` if empty |
| `Boolean` | `True` if `request.form[name] == "true"`, else `False` |
| `Date` | Parses `DD/MM/YYYY` → Python `date`, or `None` |
| `DateTime` | Parses `DD/MM/YYYY, HH:MM` → Python `datetime`, or `None` |
| `Password` | Hashes the value with `generate_password_hash()` |
| `Color` | Hex string from hidden input |
| `ManyToOne` | Integer id (or list of one id) → resolved to model instance |
| `ManyToMany`, `OneToMany` | List of integer ids from `request.form.getlist(name)` |
| `Picture`, `EditablePicture` | Saves file to GCS, creates `Image` record, stores `image.id` |
| `MultiplePictures` | Same as above for each file; stores list of image ids |

`update_with_dict` then applies each value to the instance. Relationship fields are resolved to instances automatically. Pass a field name in `_replace_collections` if you want a many-to-many to be replaced rather than appended:

```python
instance.update_with_dict(values, _replace_collections={"tags"})
```

---

## Image fields

- **`Picture`** — single upload. The image is saved to GCS at `images/<model>/<timestamp>_<filename>`. An `Image` DB record is created and its `id` is stored in the column.
- **`EditablePicture`** — same as `Picture` but opens a canvas-based crop/scale modal before saving.
- **`MultiplePictures`** — allows multiple files; stores a list of image ids.

Image fields must go in the `"picture_block"`, not the `"info_block"`. Any other block name raises a `ValueError`.

To override the GCS path for a picture field:

```python
Field(..., type="Picture", mandatory_path="covers/hero.jpg")
```

---

## Tabs

Use `Tab` when you want to group fields into sub-sections without adding a second block:

```python
from padel_app.tools.input_tools import Block, Field, Form, Tab

@classmethod
def get_create_form(cls):
    form = Form()
    form.add_block(Block("info_block", fields=[
        Field(..., name="name", type="Text", label="Name"),
    ]))
    form.add_tab(Tab("Advanced", fields=[
        Field(..., name="settings", type="Text", label="Settings"),
    ]))
    return form
```

Tabs accept an optional `orientation` parameter (`"vertical"` or `"horizontal"`) that controls the CSS layout.

---

## Complete example

```python
# padel_app/models/coach_level.py

from sqlalchemy import Column, ForeignKey, Integer, String
from sqlalchemy.orm import relationship

from padel_app.sql_db import db
from padel_app import model


class CoachLevel(db.Model, model.Model):
    __tablename__ = "coach_levels"
    __table_args__ = {"extend_existing": True}

    page_title = "Coach Levels"
    model_name = "CoachLevel"

    id            = Column(Integer, primary_key=True)
    coach_id      = Column(Integer, ForeignKey("coaches.id", ondelete="CASCADE"), nullable=False)
    label         = Column(String(100), nullable=False)
    code          = Column(String(10),  nullable=False)
    display_order = Column(Integer, default=0)

    coach = relationship("Coach", back_populates="levels")

    @property
    def name(self):
        return self.label

    @classmethod
    def display_all_info(cls):
        searchable = {"field": "label", "label": "Level"}
        columns = [
            {"field": "coach",         "label": "Coach"},
            {"field": "label",         "label": "Level"},
            {"field": "code",          "label": "Code"},
            {"field": "display_order", "label": "Order"},
        ]
        return searchable, columns

    @classmethod
    def get_create_form(cls):
        from padel_app.tools.input_tools import Block, Field, Form
        form = Form()
        form.add_block(Block("info_block", fields=[
            Field(instance_id=cls.id, model=cls.model_name,
                  name="coach", label="Coach", type="ManyToOne",
                  related_model="Coach", required=True),
            Field(instance_id=cls.id, model=cls.model_name,
                  name="label", label="Level label", type="Text", required=True),
            Field(instance_id=cls.id, model=cls.model_name,
                  name="code", label="Level code", type="Text", required=True),
            Field(instance_id=cls.id, model=cls.model_name,
                  name="display_order", label="Display order", type="Integer"),
        ]))
        return form
```

```python
# padel_app/models/__init__.py
from .coach_level import CoachLevel

MODELS = {
    ...
    "coachlevel": CoachLevel,   # lowercase, no underscores
}
```

That is all that's needed. The list page, detail page, create form, edit form, delete, CSV export/import, and inline relationship modals all work automatically.

---

## Checklist

- [ ] `display_all_info()` defined — returns `(searchable, columns)`
- [ ] `searchable["field"]` is a real attribute on the model instance
- [ ] `get_create_form()` defined — covers every editable column and relationship
- [ ] Image fields are in `"picture_block"`, all other fields in `"info_block"` or a Tab
- [ ] `page_title` and `model_name` set on the class
- [ ] `name` property defined (used for `__str__` in relationship dropdowns)
- [ ] Model added to `MODELS` dict with a lowercase, no-underscore key
- [ ] Migration run and applied (`flask db migrate` + `flask db upgrade`)
