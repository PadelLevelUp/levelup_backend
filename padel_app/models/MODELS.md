# Creating a Model

All models live in `padel_app/models/`. This document covers everything you need to create one correctly.

---

## 1. File and class skeleton

Create a new file, e.g. `padel_app/models/your_model.py`:

```python
from sqlalchemy import Column, ForeignKey, Integer, String
from sqlalchemy.orm import relationship

from padel_app.sql_db import db
from padel_app import model


class YourModel(db.Model, model.Model):
    __tablename__ = "your_models"
    __table_args__ = {"extend_existing": True}

    # Required class attributes
    page_title = "Your Models"   # shown in admin list view
    model_name = "YourModel"     # used for URL routing and reflection

    # Primary key — always Integer
    id = Column(Integer, primary_key=True)

    # Your columns here...
    name = Column(String(255), nullable=False)

    @property
    def name(self):          # __repr__ and __str__ call self.name
        return self.name     # rename this property if your display field differs

    @classmethod
    def display_all_info(cls):
        searchable = {"field": "name", "label": "Name"}
        fields = [
            {"field": "name", "label": "Name"},
        ]
        return searchable, fields

    @classmethod
    def get_create_form(cls):
        from padel_app.tools.input_tools import Block, Field, Form
        form = Form()
        form.add_block(Block("info_block", fields=[
            Field(instance_id=cls.id, model=cls.model_name,
                  name="name", label="Name", type="Text", required=True),
            Field(instance_id=cls.id, model=cls.model_name,
                  name="status", label="Status", type="Select",
                  options=["pending", "active", "closed"]),
            Field(instance_id=cls.id, model=cls.model_name,
                  name="coach", label="Coach", type="ManyToOne",
                  related_model="Coach"),
        ]))
        return form
```

### Required pieces

| Piece | Why |
|---|---|
| `db.Model, model.Model` | Gives you the DB table + all session helpers |
| `__tablename__` | The actual PostgreSQL table name. Use snake_case plural. |
| `__table_args__ = {"extend_existing": True}` | Prevents errors when the module is imported multiple times |
| `page_title` | Label shown in the admin editor list page |
| `model_name` | Used by `update_with_dict`, reflection, and admin URLs. Must match the class name exactly. |
| `id = Column(Integer, primary_key=True)` | Every model needs a surrogate PK |
| `display_all_info()` | Required by the admin list view |
| `get_create_form()` | Required by the admin create/edit view — must include **every editable field** |

### Free from `model.Model`

You get these automatically — do not redefine them:

- `created_at`, `updated_at` — auto-set timestamps
- `create()` — `db.session.add(self) + commit()`
- `save()` — updates `updated_at` and commits
- `delete()` — removes from DB and commits
- `flush()`, `refresh()`, `expire()`, `merge()` — session helpers
- `update_with_dict(values)` — bulk-update columns and relationships from a dict
- `get_dict()` — returns all column values as a plain dict
- `get_edit_form()` — delegates to `get_create_form()` with pre-filled values

---

## 2. Column types

```python
from sqlalchemy import Boolean, Column, DateTime, Enum, ForeignKey, Integer, JSON, String, Text

# Text
name      = Column(String(255), nullable=False)
bio       = Column(Text, nullable=True)

# Numbers
order     = Column(Integer, nullable=False, default=0)

# Boolean
is_active = Column(Boolean, default=True, nullable=False)

# DateTime
starts_at = Column(DateTime, nullable=True)

# Enum — define the name to avoid Postgres type conflicts
status = Column(
    Enum("pending", "active", "closed", name="your_status_enum"),
    nullable=False,
    server_default="pending",
)

# JSON — for flexible structured data (lists, dicts)
settings  = Column(JSON, nullable=True)
tags      = Column(JSON, nullable=True, default=list)
```

---

## 3. `get_create_form()` — the admin form

`get_create_form()` is called by the admin editor for both create and edit views (`get_edit_form()` delegates to it). **It must declare every field that a user should be able to set.** Fields omitted here will be invisible in the admin and ignored by `update_with_dict`.

### Structure: Form → Blocks / Tabs → Fields

```
Form
├── Block("info_block")    ← regular fields (text, numbers, booleans, relationships…)
├── Block("picture_block") ← image upload fields only
└── Tab("Tab title")       ← alternative to blocks; use for grouped sub-sections
```

**Only two block names are valid:** `"info_block"` and `"picture_block"`. Any other name raises a `ValueError`.

```python
@classmethod
def get_create_form(cls):
    from padel_app.tools.input_tools import Block, Field, Form, Tab
    form = Form()

    form.add_block(Block("info_block", fields=[
        # one Field() per editable column or relationship
    ]))

    # Optional — only if the model has image fields
    form.add_block(Block("picture_block", fields=[
        # Picture / MultiplePictures fields only
    ]))

    # Optional — group extra fields into tabs instead of one flat block
    form.add_tab(Tab("Advanced", fields=[
        # more Field() entries
    ]))

    return form
```

### `Field` — all parameters

```python
Field(
    instance_id=cls.id,      # always cls.id
    model=cls.model_name,    # always cls.model_name
    name="field_name",       # must match the column or relationship name on the model
    label="Human Label",     # shown in the UI
    type="...",              # see type reference below — required
    value=None,              # pre-filled value (leave None for create forms)
    options=None,            # list of strings — required for Select
    required=False,          # marks field as mandatory in the UI
    related_model=None,      # class name string — required for relationship types
    mandatory_path=None,     # GCS path override — only for Picture types
)
```

### Field type reference

| Type | Use for | Extra required params |
|---|---|---|
| `"Text"` | `String`, `Text` columns | — |
| `"Integer"` | `Integer` columns | — |
| `"Float"` | `Float` columns | — |
| `"Boolean"` | `Boolean` columns | — |
| `"Date"` | `DateTime` columns where only date matters | — |
| `"DateTime"` | `DateTime` columns with time | — |
| `"Password"` | Password fields (auto-hashed on save) | — |
| `"Color"` | Hex color string columns | — |
| `"Select"` | `Enum` columns or any fixed-choice string | `options=["a", "b", "c"]` |
| `"ManyToOne"` | FK column / belongs-to relationship | `related_model="ModelName"` |
| `"ManyToMany"` | Many-to-many relationship | `related_model="ModelName"` |
| `"OneToMany"` | One-to-many relationship | `related_model="ModelName"` |
| `"Picture"` | Single image upload (stores `image_id`) | — |
| `"EditablePicture"` | Single image that can be replaced | — |
| `"MultiplePictures"` | Multiple image uploads | — |

### Complete example

```python
@classmethod
def get_create_form(cls):
    from padel_app.tools.input_tools import Block, Field, Form, Tab

    form = Form()

    form.add_block(Block("info_block", fields=[
        # Plain text
        Field(instance_id=cls.id, model=cls.model_name,
              name="name", label="Name", type="Text", required=True),

        # Fixed choices (Enum column)
        Field(instance_id=cls.id, model=cls.model_name,
              name="status", label="Status", type="Select",
              options=["pending", "active", "closed"]),

        # Number
        Field(instance_id=cls.id, model=cls.model_name,
              name="capacity", label="Capacity", type="Integer"),

        # Boolean
        Field(instance_id=cls.id, model=cls.model_name,
              name="is_active", label="Active", type="Boolean"),

        # Date / DateTime
        Field(instance_id=cls.id, model=cls.model_name,
              name="starts_at", label="Start date", type="Date"),
        Field(instance_id=cls.id, model=cls.model_name,
              name="created_at", label="Created at", type="DateTime"),

        # Belongs-to relationship (FK column)
        Field(instance_id=cls.id, model=cls.model_name,
              name="coach", label="Coach", type="ManyToOne",
              related_model="Coach"),

        # Many-to-many relationship
        Field(instance_id=cls.id, model=cls.model_name,
              name="players", label="Players", type="ManyToMany",
              related_model="Player"),
    ]))

    # Only add this block if the model has image columns
    form.add_block(Block("picture_block", fields=[
        Field(instance_id=cls.id, model=cls.model_name,
              name="cover_image", label="Cover image", type="Picture"),
    ]))

    return form
```

---

## 4. Foreign keys and relationships

### Many-to-one (this model belongs to another)

```python
coach_id = Column(Integer, ForeignKey("coaches.id", ondelete="CASCADE"), nullable=False)
coach    = relationship("Coach", back_populates="your_models")
```

On the `Coach` side:
```python
your_models = relationship("YourModel", back_populates="coach", cascade="all, delete-orphan")
```

Use `ondelete="CASCADE"` when rows should be deleted with the parent.
Use `ondelete="SET NULL"` (+ `nullable=True`) when you want to keep the row orphaned.

### One-to-many (this model owns children)

```python
items = relationship("Item", back_populates="your_model", cascade="all, delete-orphan")
```

### Many-to-many — with extra columns on the junction

Use an `Association_` model (see section 4).

### Many-to-many — no extra columns

Use a `Table` object as `secondary`:

```python
from sqlalchemy import Table

your_model_tags = Table(
    "your_model_tags",
    db.Model.metadata,
    Column("your_model_id", Integer, ForeignKey("your_models.id", ondelete="CASCADE"), primary_key=True),
    Column("tag_id",        Integer, ForeignKey("tags.id",         ondelete="CASCADE"), primary_key=True),
)

class YourModel(db.Model, model.Model):
    ...
    tags = relationship("Tag", secondary=your_model_tags, back_populates="your_models")
```

### Ordered relationships

```python
history = relationship(
    "HistoryEntry",
    back_populates="your_model",
    cascade="all, delete-orphan",
    order_by="desc(HistoryEntry.created_at)",
)
```

### Multiple FKs to the same table

```python
home_team_id = Column(Integer, ForeignKey("teams.id"))
away_team_id = Column(Integer, ForeignKey("teams.id"))

home_team = relationship("Team", foreign_keys=[home_team_id])
away_team = relationship("Team", foreign_keys=[away_team_id])
```

---

## 5. Association models (junction tables with extra data)

When a many-to-many link carries extra columns, use a dedicated model prefixed `Association_`:

```python
# padel_app/models/Association_YourModelTag.py
from sqlalchemy import Column, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import relationship

from padel_app.sql_db import db
from padel_app import model


class Association_YourModelTag(db.Model, model.Model):
    __tablename__ = "your_model_tags"
    __table_args__ = (
        UniqueConstraint("your_model_id", "tag_id", name="uq_your_model_tag"),
        {"extend_existing": True},
    )

    page_title = "YourModel ↔ Tag"
    model_name = "Association_YourModelTag"

    id           = Column(Integer, primary_key=True)
    your_model_id = Column(Integer, ForeignKey("your_models.id", ondelete="CASCADE"))
    tag_id        = Column(Integer, ForeignKey("tags.id",         ondelete="CASCADE"))

    # Extra columns
    added_by = Column(String(80), nullable=True)

    your_model = relationship("YourModel", back_populates="tags_relations")
    tag        = relationship("Tag",       back_populates="your_models_relations")

    @classmethod
    def display_all_info(cls): ...
    @classmethod
    def get_create_form(cls): ...
```

Expose a clean property on the parent model:

```python
class YourModel(db.Model, model.Model):
    tags_relations = relationship("Association_YourModelTag", back_populates="your_model", cascade="all, delete-orphan")

    @property
    def tags(self):
        return [r.tag for r in self.tags_relations]
```

---

## 6. Default values for JSON columns

When a JSON column may be `None` (never written), provide defaults through a getter method rather than relying on the column default alone:

```python
DEFAULT_SETTINGS = {"theme": "light", "notifications": True}

class YourModel(db.Model, model.Model):
    settings = Column(JSON, nullable=True)

    def get_settings(self):
        if self.settings is None:
            return DEFAULT_SETTINGS
        # Merge so new keys always appear even on old rows
        return {**DEFAULT_SETTINGS, **self.settings}
```

This pattern is used by `NotificationConfig` and is the standard way to handle evolving JSON schemas.

---

## 7. Register the model

### `models/__init__.py`

Add the import and a `MODELS` entry:

```python
from .your_model import YourModel

MODELS = {
    ...
    "yourmodel": YourModel,   # lowercase, no underscores
}
```

The `MODELS` key is used by the admin editor for URL routing. Convention: lowercase class name, no underscores.

---

## 8. Migration

After defining the model, generate and apply a migration:

```bash
# From levelup_backend/
flask db migrate -m "add YourModel"
flask db upgrade
```

The generated file lands in `migrations/versions/`. Always review it before running `upgrade` — Alembic occasionally misses things (e.g. Enum type creation on Postgres).

### Adding columns to an existing table

```python
def upgrade():
    with op.batch_alter_table("your_models", schema=None) as batch_op:
        batch_op.add_column(sa.Column("new_field", sa.String(255), nullable=True))
        batch_op.add_column(sa.Column("metadata",  sa.JSON(),       nullable=True))

def downgrade():
    with op.batch_alter_table("your_models", schema=None) as batch_op:
        batch_op.drop_column("metadata")
        batch_op.drop_column("new_field")
```

### Creating a new Enum type on Postgres

Alembic won't auto-create named Enum types. Add this explicitly:

```python
from alembic import op
import sqlalchemy as sa

your_status = sa.Enum("pending", "active", "closed", name="your_status_enum")

def upgrade():
    your_status.create(op.get_bind(), checkfirst=True)
    op.create_table("your_models", ..., sa.Column("status", your_status, ...))

def downgrade():
    op.drop_table("your_models")
    your_status.drop(op.get_bind(), checkfirst=True)
```

---

## 9. Checklist

- [ ] Class inherits `db.Model, model.Model`
- [ ] `__tablename__` is snake_case plural
- [ ] `__table_args__ = {"extend_existing": True}`
- [ ] `page_title` and `model_name` set
- [ ] `id = Column(Integer, primary_key=True)`
- [ ] All FKs have an `ondelete` policy (`CASCADE` or `SET NULL`)
- [ ] All bidirectional relationships use `back_populates` (not `backref`)
- [ ] `display_all_info()` implemented
- [ ] `get_create_form()` implemented with a `Field` for **every editable column and relationship**
- [ ] Correct `Field` type used for each column (see type reference in section 3)
- [ ] `Select` fields have `options=[...]`
- [ ] Relationship fields (`ManyToOne`, `ManyToMany`, `OneToMany`) have `related_model="ClassName"`
- [ ] Image fields are in `"picture_block"`, not `"info_block"`
- [ ] Import added to `models/__init__.py`
- [ ] Entry added to `MODELS` dict in `__init__.py`
- [ ] `flask db migrate` run and migration file reviewed
- [ ] `flask db upgrade` applied
