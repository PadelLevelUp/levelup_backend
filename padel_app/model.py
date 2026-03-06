import os
from datetime import timedelta, datetime

from flask import url_for
from google.cloud import storage
from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    ForeignKey,
    Integer,
    String,
    DateTime,
    inspect,
)
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import class_mapper, relationship

from .sql_db import db

GCS_BUCKET = os.environ.get("GCS_UPLOADS_BUCKET")
PUBLIC_BASE = f"https://storage.googleapis.com/{GCS_BUCKET}"


class Model:

    _name = None
    _description = None
    __tablename__ = None

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)

    def __repr__(self):
        try:
            return f"{self.model_name}: {self.name}"
        except ValueError:
            ValueError(
                "This model has no defined name, choose another way of representing"
            )

    def __str__(self):
        try:
            return f"{self.model_name}: {self.name}"
        except ValueError:
            ValueError(
                "This model has no defined name, choose another way of representing"
            )

    def create(self):
        if hasattr(self, "_sa_instance_state"):
            self._sa_instance_state.key = None
        if inspect(self).key is not None:
            inspect(self).key = None
        db.session.add(self)
        db.session.commit()
        return True

    def add_to_session(self):
        db.session.add(self)
        return True

    def delete(self):
        db.session.delete(self)
        db.session.commit()
        return True

    def save(self):
        self.updated_at = datetime.now()
        db.session.commit()
        return True

    def logout(self):
        db.session.expunge_all()
        db.session.close()
        return True

    def refresh(self):
        db.session.refresh(self)
        return True

    def expire(self):
        db.session.expire(self)
        return True

    def merge(self):
        new = db.session.merge(self)
        db.session.commit()
        return new

    def flush(self):
        db.session.flush([self])
        return True

    def get_table(self, model):
        return db.session.query(self.table_object(table_name=model))

    def table_object(self, table_name):
        tables_dict = {
            table.__tablename__: table for table in db.Model.__subclasses__()
        }
        return tables_dict.get(table_name)

    def all_tables_object(self):
        return {table.__tablename__: table for table in db.Model.__subclasses__()}

    def get_all_tables(self):
        return {
            table.__tablename__: db.session.query(table)
            for table in db.Model.__subclasses__()
        }

    def update_with_dict(
        self, values: dict, *, _replace_collections: set[str] = frozenset()
    ) -> bool:
        """
        Update this instance from a values dict.

        - Relationships are only updated when the incoming value is *truthy* (preserves your old semantics).
        - MANYTOONE accepts an id, [id], or an instance.
        - Collection rels (MANYTOMANY / ONETOMANY) append missing items (no removals).
        Pass names in `_replace_collections` to replace instead of append.
        - Columns: booleans set when changed; other columns set when non-None and changed.
        """
        with db.session.no_autoflush:
            mapper = inspect(self.__class__)
            rel_map = {rel.key: rel for rel in class_mapper(type(self)).relationships}

            for key, incoming in values.items():
                if key in rel_map:
                    if not incoming:
                        continue
                    self._apply_relationship(
                        key,
                        incoming,
                        rel_map[key],
                        mapper,
                        replace=(key in _replace_collections),
                    )
                else:
                    self._apply_column(key, incoming, mapper)
        return True

    # ---------- helpers ----------

    def _apply_relationship(
        self, key, incoming, relationship, mapper, *, replace: bool
    ) -> None:
        direction = (
            relationship.direction.name
        )  # 'MANYTOONE' | 'ONETOMANY' | 'MANYTOMANY'
        if direction == "MANYTOONE":
            self._apply_many_to_one(key, incoming, relationship, mapper)
        elif direction in ("MANYTOMANY", "ONETOMANY"):
            self._apply_collection(key, incoming, relationship, replace=replace)

    def _resolve_instance(self, relationship, incoming):
        """Return ORM instance from id/[id]/instance; None if not found."""
        if hasattr(incoming, "__mapper__"):
            return incoming
        if isinstance(incoming, (list, tuple)):
            incoming = incoming[0] if incoming else None
        if incoming is None:
            return None
        cls = relationship.mapper.class_
        return cls.query.filter_by(id=incoming).first()

    def _apply_many_to_one(self, key, incoming, relationship, mapper) -> None:
        inst = self._resolve_instance(relationship, incoming)
        if inst is None:
            return
        if getattr(self, key) is not inst:
            setattr(self, key, inst)

        fk_attr = f"{key}_id"
        if mapper.columns.get(fk_attr) is not None:
            if getattr(self, fk_attr, None) != getattr(inst, "id", None):
                setattr(self, fk_attr, getattr(inst, "id", None))

    def _apply_collection(self, key, incoming, relationship, *, replace: bool) -> None:
        cls = relationship.mapper.class_
        if incoming and hasattr(incoming[0], "__mapper__"):
            instances = list(incoming)
        else:
            ids = list(incoming) if isinstance(incoming, (list, tuple)) else [incoming]
            instances = cls.query.filter(cls.id.in_(ids)).all()

        coll = getattr(self, key)
        if replace:
            coll.clear()
            coll.extend(instances)
            return

        existing = set(coll)
        for inst in instances:
            if inst not in existing:
                coll.append(inst)

    def _apply_column(self, key, incoming, mapper) -> None:
        column = mapper.columns.get(key)
        if column is None:
            return

        current = getattr(self, key)
        if isinstance(column.type, Boolean):
            if incoming != current:
                setattr(self, key, incoming)
            return

        if incoming is not None and incoming != current:
            setattr(self, key, incoming)

    def get_related_object(self, field_name):
        return getattr(self.__class__, field_name).property.mapper.entity

    def get_create_form(self):
        # Define this method in each derived class
        raise NotImplementedError

    def display_all_info(self):
        # Define this method in each derived class
        raise NotImplementedError

    def get_basic_create_form(self):
        # Define this method in each derived class
        raise NotImplementedError

    def get_display_all_data(self, page=1, per_page=100):
        model_class = type(self)
        sort_order = getattr(self, "sort_order", "oldest")
        if sort_order == "newest":
            ordered_query = self.query.order_by(model_class.id.desc())
        else:
            ordered_query = self.query.order_by(model_class.id.asc())
        searchable_column, table_columns = self.display_all_info()
        pagination = ordered_query.paginate(
            page=page, per_page=per_page, error_out=False
        )
        data = {
            "dispalay_all_url": url_for("editor.display_all", model=self.model_name),
            "title": self.page_title,
            "create_url": url_for("editor.create", model=self.model_name),
            "searchable_column": searchable_column,
            "table_columns": table_columns,
            "objects": pagination.items,
            "pagination": pagination,
            "general_delete_url": url_for("api.delete", model=self.model_name, id=""),
            "download_csv_url": url_for("api.download_csv", model=self.model_name),
            "upload_csv_url": url_for("api.upload_csv_to_db", model=self.model_name),
        }
        return data

    def get_edit_form(self):
        form = self.get_create_form()
        for field in form.fields:
            field.value = getattr(self, field.name)
        return form

    def get_display_data(self):
        data = {
            "dispalay_all_url": url_for("editor.display_all", model=self.model_name),
            "title": self.page_title,
            "post_url": url_for("api.edit", model=self.model_name, id=self.id),
            "delete_url": url_for("api.delete", model=self.model_name, id=self.id),
            "form": self.get_edit_form().get_form_dict(),
        }
        return data

    def get_create_data(self, form=None):
        if not form:
            form = self.get_create_form()
        data = {
            "dispalay_all_url": url_for("editor.display_all", model=self.model_name),
            "title": self.page_title,
            "post_url": url_for("editor.create", model=self.model_name),
            "form": form.get_form_dict(),
        }
        return data

    def get_basic_create_data(self, form=None):
        if not form:
            form = self.get_basic_create_form()
        data = {
            "post_url": url_for("api.modal_create_page", model=self.model_name),
            "form": form.get_form_dict(),
        }
        return data

    def editor_url(self):
        return url_for("editor.display", model=self.model_name, id=self.id)

    def get_dict(self):
        instance_dict = {c.name: getattr(self, c.name) for c in self.__table__.columns}

        for key, value in self.__class__.__dict__.items():
            if isinstance(value, hybrid_property):
                instance_dict[key] = getattr(self, key)
        return instance_dict

    def get_model_names(self):
        return [
            obj.model_name
            for obj in self.all_tables_object().values()
            if hasattr(obj, "model_name")
        ]


class Image(db.Model):
    __tablename__ = "images"
    id = Column(Integer, primary_key=True)

    object_key = Column(String(512), nullable=False, unique=True)
    content_type = Column(String(128))
    size_bytes = Column(BigInteger)
    is_public = Column(Boolean, nullable=False, default=True)

    imageable_id = Column(
        Integer, ForeignKey("imageables.imageable_id", ondelete="CASCADE")
    )
    imageable = relationship("Imageable", back_populates="images", cascade="all")

    def create(self):
        db.session.add(self)
        db.session.commit()
        return True

    def add_to_session(self):
        db.session.add(self)
        return True

    def delete(self):
        db.session.delete(self)
        db.session.commit()
        return True

    def save(self):
        db.session.commit()
        return True

    def _blob(self):
        return storage.Client().bucket(GCS_BUCKET).blob(self.object_key)

    def public_url(self):
        return f"{PUBLIC_BASE}/{self.object_key}"

    def signed_url(self, minutes=5, method="GET"):
        return self._blob().generate_signed_url(
            version="v4", expiration=timedelta(minutes=minutes), method=method
        )

    def url(self):
        return self.public_url() if self.is_public else self.signed_url()


class Imageable(db.Model):
    __tablename__ = "imageables"
    imageable_id = Column(Integer, primary_key=True)
    type = Column(String(50))
    __mapper_args__ = {"polymorphic_identity": "imageable", "polymorphic_on": type}
    images = relationship("Image", back_populates="imageable", cascade="all")

    def create(self):
        db.session.add(self)
        db.session.commit()
        return True

    def delete(self):
        db.session.expunge(self)
        db.session.delete(self)
        db.session.commit()
        return True
