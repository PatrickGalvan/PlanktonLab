"""Database models, schema introspection, query construction, and data export.

The public helpers are documented with payload definitions and examples in
``docs/API_REFERENCE.md``. Database operations require an active Flask application
context.
"""

from apps import app
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    and_,
    event,
    func,
    inspect,
    select,
    text,
)
from sqlalchemy.engine import Engine
from datetime import date, datetime, timezone
from typing import Optional
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
)
from collections import defaultdict
from base64 import b64decode
from PIL import Image, ImageEnhance, ImageFilter, ImageOps
import hashlib
import io
import json
import random
import re
import sqlite3
import time
import zipfile
import numpy as np

# Dictionary to map table names to their respective SQLAlchemy model classes
classes = {}

QUERY_TIMEOUT_SECONDS = 3


class ReadOnlyQueryError(ValueError):
    """Raised when an advanced query is not a safe, single read-only statement."""


def _sql_structure(sql):
    """Return SQL with comments and quoted content replaced by spaces.

    Keeping token positions while removing quoted content lets validation inspect
    statement structure without rejecting harmless words inside string literals.
    """
    output = []
    index = 0
    quote = None

    while index < len(sql):
        char = sql[index]
        next_char = sql[index + 1] if index + 1 < len(sql) else ""

        if quote:
            output.append(" ")
            if char == quote:
                if next_char == quote:
                    output.append(" ")
                    index += 2
                    continue
                quote = None
            index += 1
            continue

        if char in ("'", '"', "`"):
            quote = char
            output.append(" ")
            index += 1
            continue

        if char == "[":
            quote = "]"
            output.append(" ")
            index += 1
            continue

        if char == "-" and next_char == "-":
            while index < len(sql) and sql[index] not in "\r\n":
                output.append(" ")
                index += 1
            continue

        if char == "/" and next_char == "*":
            output.extend((" ", " "))
            index += 2
            while index < len(sql):
                if sql[index] == "*" and index + 1 < len(sql) and sql[index + 1] == "/":
                    output.extend((" ", " "))
                    index += 2
                    break
                output.append(" ")
                index += 1
            continue

        output.append(char)
        index += 1

    if quote:
        raise ReadOnlyQueryError("The SQL contains an unterminated quoted value.")

    return "".join(output)


def validate_read_only_query(sql):
    """Validate that *sql* is one read-only ``SELECT`` or ``WITH`` statement."""
    if not isinstance(sql, str) or not sql.strip():
        raise ReadOnlyQueryError("Enter a SQL query before running it.")
    if len(sql) > 20_000:
        raise ReadOnlyQueryError("The SQL query is too long.")

    structure = _sql_structure(sql).strip()
    if structure.endswith(";"):
        structure = structure[:-1].rstrip()
    if ";" in structure:
        raise ReadOnlyQueryError("Run one SQL statement at a time.")

    tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", structure.upper())
    if not tokens or tokens[0] not in {"SELECT", "WITH"}:
        raise ReadOnlyQueryError("Only SELECT queries and SELECT-based CTEs are allowed.")

    forbidden = {
        "ALTER", "ANALYZE", "ATTACH", "CREATE", "DELETE", "DETACH", "DROP",
        "INSERT", "PRAGMA", "REINDEX", "REPLACE", "TRUNCATE", "UPDATE", "VACUUM",
    }
    blocked = next((token for token in tokens if token in forbidden), None)
    if blocked:
        raise ReadOnlyQueryError(f"{blocked} is not allowed in read-only SQL mode.")

    return sql.strip()


def execute_read_only_query(sql):
    """Execute guarded SQL and return every matching row.

    Results are mappings keyed by the selected column names. SQLite connections
    additionally use ``query_only`` and a short progress-handler timeout as
    defense in depth.
    """
    statement = validate_read_only_query(sql)

    with db.engine.connect() as connection:
        raw_connection = getattr(connection.connection, "driver_connection", None)
        deadline = time.monotonic() + QUERY_TIMEOUT_SECONDS

        if db.engine.dialect.name == "sqlite":
            connection.exec_driver_sql("PRAGMA query_only = ON")
            if raw_connection is not None:
                raw_connection.set_progress_handler(
                    lambda: int(time.monotonic() > deadline), 1_000
                )

        try:
            result = connection.execute(text(statement))
            if not result.returns_rows:
                raise ReadOnlyQueryError("The query must return rows.")
            fetched = result.mappings().all()
        finally:
            if raw_connection is not None and hasattr(raw_connection, "set_progress_handler"):
                raw_connection.set_progress_handler(None, 0)
            if db.engine.dialect.name == "sqlite":
                connection.exec_driver_sql("PRAGMA query_only = OFF")

    return [dict(row) for row in fetched]


class Base(DeclarativeBase):
    """
    Custom Base class for SQLAlchemy models defining a naming convention
    for predictable constraints and indexes.
    """

    metadata = MetaData(
        naming_convention={
            "ix": "ix_%(column_0_label)s",
            "uq": "uq_%(table_name)s_%(column_0_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        }
    )


db = SQLAlchemy(app, model_class=Base)


@event.listens_for(Engine, "connect")
def enable_sqlite_foreign_keys(dbapi_connection, _connection_record):
    """Enable foreign-key enforcement for every SQLite connection."""
    if isinstance(dbapi_connection, sqlite3.Connection):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys = ON")
        cursor.close()

### --- DATABASE MODELS --- ###


def utc_now():
    """Return the current timezone-aware UTC timestamp."""
    return datetime.now(timezone.utc)


class TimestampMixin:
    """Add creation and last-update timestamps to persisted records."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        server_default=func.current_timestamp(),
        onupdate=utc_now,
    )


class User(TimestampMixin, db.Model):
    """Represents application users."""

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(unique=True)
    email: Mapped[str]


class Lakes(TimestampMixin, db.Model):
    """Represents geographical lake locations and metadata."""

    __table_args__ = (
        UniqueConstraint("lake", "country", name="uq_lakes_lake_country"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    lake: Mapped[str] = mapped_column(nullable=False)
    country: Mapped[str] = mapped_column(nullable=False)
    depth: Mapped[Optional[float]] = mapped_column(Float)
    area: Mapped[Optional[float]] = mapped_column(Float)
    latitude: Mapped[Optional[float]] = mapped_column(Float)
    longitude: Mapped[Optional[float]] = mapped_column(Float)
    elevation_m: Mapped[Optional[float]] = mapped_column(Float)
    mean_depth_m: Mapped[Optional[float]] = mapped_column(Float)
    volume_km3: Mapped[Optional[float]] = mapped_column(Float)
    shoreline_length_km: Mapped[Optional[float]] = mapped_column(Float)
    catchment_area_km2: Mapped[Optional[float]] = mapped_column(Float)
    residence_time_days: Mapped[Optional[float]] = mapped_column(Float)
    waterbody_type: Mapped[Optional[str]] = mapped_column(String(80))
    administrative_region: Mapped[Optional[str]] = mapped_column(String(160))
    external_source: Mapped[Optional[str]] = mapped_column(String(40))
    external_id: Mapped[Optional[str]] = mapped_column(String(120))
    source_url: Mapped[Optional[str]] = mapped_column(Text)
    metadata_retrieved_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True)
    )


class Taxon(TimestampMixin, db.Model):
    """Normalized taxonomic label used by predictions and human reviews."""

    __tablename__ = "taxa"
    __table_args__ = (
        UniqueConstraint("scientific_name", name="uq_taxa_scientific_name"),
        Index("ix_taxa_rank", "rank"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    scientific_name: Mapped[str] = mapped_column(String(255), nullable=False)
    common_name: Mapped[Optional[str]] = mapped_column(String(255))
    rank: Mapped[str] = mapped_column(String(50), nullable=False, default="species")
    parent_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("taxa.id", ondelete="SET NULL")
    )

    parent = db.relationship("Taxon", remote_side=[id], backref="children")


class ModelVersion(TimestampMixin, db.Model):
    """Identifies a classifier artifact and its evaluation metadata."""

    __tablename__ = "model_versions"
    __table_args__ = (
        UniqueConstraint("name", "version", name="uq_model_versions_name_version"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    version: Mapped[str] = mapped_column(String(80), nullable=False)
    artifact_path: Mapped[Optional[str]] = mapped_column(String(500))
    metrics_json: Mapped[Optional[str]] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class Sampling_metadata(TimestampMixin, db.Model):
    """Stores metadata regarding a specific sampling event and the associated raw image."""

    __table_args__ = (
        Index("ix_sampling_metadata_user_id", "user_id"),
        Index("ix_sampling_metadata_lake_id", "lake_id"),
        Index("ix_sampling_metadata_sampling_date", "sampling_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        db.ForeignKey("user.id", ondelete="RESTRICT"), nullable=False
    )
    lake_id: Mapped[int] = mapped_column(
        db.ForeignKey("lakes.id", ondelete="RESTRICT"), nullable=False
    )
    image_name: Mapped[str] = mapped_column(nullable=False, unique=True)
    sampling_date: Mapped[date] = mapped_column(Date, nullable=False)
    image_path: Mapped[str] = mapped_column(nullable=False)

    user = db.relationship("User")
    lake = db.relationship("Lakes")


class ModelRun(TimestampMixin, db.Model):
    """Tracks one model execution against a source sampling image."""

    __tablename__ = "model_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'completed', 'failed')",
            name="model_runs_status",
        ),
        Index("ix_model_runs_sampling_metadata_id", "sampling_metadata_id"),
        Index("ix_model_runs_status", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    model_version_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("model_versions.id", ondelete="SET NULL")
    )
    sampling_metadata_id: Mapped[int] = mapped_column(
        ForeignKey("sampling_metadata.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="running")
    parameters_json: Mapped[Optional[str]] = mapped_column(Text)
    error_message: Mapped[Optional[str]] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, server_default=func.current_timestamp()
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    model_version = db.relationship("ModelVersion")
    sampling_metadata = db.relationship("Sampling_metadata")


class Samples(TimestampMixin, db.Model):
    """Stores detections and their current classification snapshot.

    ``species`` and ``confidence`` remain for compatibility. ``taxon_id`` and
    ``model_run_id`` link that snapshot to normalized history records, while the
    crop itself remains stored in ``image_base64``.
    """

    __table_args__ = (
        CheckConstraint("x >= 0 AND y >= 0", name="samples_nonnegative_origin"),
        CheckConstraint("w > 0 AND h > 0", name="samples_positive_dimensions"),
        CheckConstraint(
            "confidence >= 0 AND confidence <= 1", name="samples_confidence_range"
        ),
        CheckConstraint(
            "review_status IN ('unreviewed', 'reviewed')",
            name="samples_review_status",
        ),
        Index("ix_samples_sampling_metadata_id", "sampling_metadata_id"),
        Index("ix_samples_species", "species"),
        Index("ix_samples_review_status", "review_status"),
        Index(
            "ix_samples_sampling_metadata_species",
            "sampling_metadata_id",
            "species",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    sampling_metadata_id: Mapped[int] = mapped_column(
        db.ForeignKey("sampling_metadata.id", ondelete="CASCADE"), nullable=False
    )
    x: Mapped[int] = mapped_column(nullable=False)
    y: Mapped[int] = mapped_column(nullable=False)
    w: Mapped[int] = mapped_column(nullable=False)
    h: Mapped[int] = mapped_column(nullable=False)
    species: Mapped[str] = mapped_column(nullable=False)
    confidence: Mapped[float] = mapped_column(nullable=False)
    image_base64: Mapped[str] = mapped_column(Text, nullable=False)
    taxon_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("taxa.id", ondelete="SET NULL")
    )
    model_run_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("model_runs.id", ondelete="SET NULL")
    )
    review_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="unreviewed", server_default="unreviewed"
    )

    sampling_metadata = db.relationship("Sampling_metadata")
    taxon = db.relationship("Taxon")
    model_run = db.relationship("ModelRun")


class Prediction(TimestampMixin, db.Model):
    """Stores an immutable model or imported classification for a detection."""

    __tablename__ = "predictions"
    __table_args__ = (
        CheckConstraint(
            "confidence >= 0 AND confidence <= 1", name="predictions_confidence_range"
        ),
        CheckConstraint(
            "source IN ('model', 'import')", name="predictions_source"
        ),
        Index("ix_predictions_sample_id", "sample_id"),
        Index("ix_predictions_taxon_id", "taxon_id"),
        Index("ix_predictions_model_run_id", "model_run_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    sample_id: Mapped[int] = mapped_column(
        ForeignKey("samples.id", ondelete="CASCADE"), nullable=False
    )
    taxon_id: Mapped[int] = mapped_column(
        ForeignKey("taxa.id", ondelete="RESTRICT"), nullable=False
    )
    model_run_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("model_runs.id", ondelete="SET NULL")
    )
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="model")

    sample = db.relationship(
        "Samples", backref=db.backref("predictions", cascade="all, delete-orphan")
    )
    taxon = db.relationship("Taxon")
    model_run = db.relationship("ModelRun")


class Review(TimestampMixin, db.Model):
    """Records an immutable human review decision for a detection."""

    __tablename__ = "reviews"
    __table_args__ = (
        CheckConstraint(
            "status IN ('accepted', 'corrected', 'rejected')",
            name="reviews_status",
        ),
        Index("ix_reviews_sample_id", "sample_id"),
        Index("ix_reviews_reviewer_id", "reviewer_id"),
        Index("ix_reviews_taxon_id", "taxon_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    sample_id: Mapped[int] = mapped_column(
        ForeignKey("samples.id", ondelete="CASCADE"), nullable=False
    )
    reviewer_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL")
    )
    taxon_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("taxa.id", ondelete="RESTRICT")
    )
    previous_species: Mapped[Optional[str]] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    notes: Mapped[Optional[str]] = mapped_column(Text)

    sample = db.relationship(
        "Samples", backref=db.backref("reviews", cascade="all, delete-orphan")
    )
    reviewer = db.relationship("User")
    taxon = db.relationship("Taxon")


# Registration of models for dynamic lookup
classes["user"] = User
classes["lakes"] = Lakes
classes["samples"] = Samples
classes["sampling_metadata"] = Sampling_metadata
classes["taxa"] = Taxon
classes["model_versions"] = ModelVersion
classes["model_runs"] = ModelRun
classes["predictions"] = Prediction
classes["reviews"] = Review


REPORTING_VIEWS = ("classification_summary", "review_queue", "model_run_summary")


def initialize_database():
    """Create the current SQLite schema and its reporting views."""
    if db.engine.dialect.name != "sqlite":
        raise RuntimeError("This application requires SQLite.")

    db.create_all()
    with db.engine.begin() as connection:
        _create_reporting_views(connection)


def _create_reporting_views(connection):
    """Create stable read models for review and reporting queries."""
    for view in REPORTING_VIEWS:
        connection.exec_driver_sql(f'DROP VIEW IF EXISTS "{view}"')

    connection.exec_driver_sql(
        """
        CREATE VIEW classification_summary AS
        SELECT
            sm.id AS sampling_metadata_id,
            sm.image_name,
            sm.sampling_date,
            l.lake,
            COALESCE(t.scientific_name, s.species) AS taxon,
            COUNT(s.id) AS detection_count,
            ROUND(AVG(s.confidence), 4) AS average_confidence,
            SUM(CASE WHEN s.review_status = 'reviewed' THEN 1 ELSE 0 END) AS reviewed_count
        FROM sampling_metadata AS sm
        JOIN lakes AS l ON l.id = sm.lake_id
        LEFT JOIN samples AS s ON s.sampling_metadata_id = sm.id
        LEFT JOIN taxa AS t ON t.id = s.taxon_id
        GROUP BY sm.id, sm.image_name, sm.sampling_date, l.lake,
                 COALESCE(t.scientific_name, s.species)
        """
    )
    connection.exec_driver_sql(
        """
        CREATE VIEW review_queue AS
        SELECT
            s.id AS sample_id,
            s.sampling_metadata_id,
            sm.image_name,
            sm.sampling_date,
            l.lake,
            s.x, s.y, s.w, s.h,
            COALESCE(t.scientific_name, s.species) AS current_taxon,
            s.confidence,
            s.review_status,
            s.image_base64,
            s.created_at,
            s.updated_at
        FROM samples AS s
        JOIN sampling_metadata AS sm ON sm.id = s.sampling_metadata_id
        JOIN lakes AS l ON l.id = sm.lake_id
        LEFT JOIN taxa AS t ON t.id = s.taxon_id
        WHERE s.review_status = 'unreviewed'
        """
    )
    connection.exec_driver_sql(
        """
        CREATE VIEW model_run_summary AS
        SELECT
            mr.id AS model_run_id,
            sm.image_name,
            mv.name AS model_name,
            mv.version AS model_version,
            mr.status,
            mr.started_at,
            mr.completed_at,
            COUNT(p.id) AS prediction_count
        FROM model_runs AS mr
        JOIN sampling_metadata AS sm ON sm.id = mr.sampling_metadata_id
        LEFT JOIN model_versions AS mv ON mv.id = mr.model_version_id
        LEFT JOIN predictions AS p ON p.model_run_id = mr.id
        GROUP BY mr.id, sm.image_name, mv.name, mv.version, mr.status,
                 mr.started_at, mr.completed_at
        """
    )


def get_or_create_taxon(species, rank="species"):
    """Return a normalized taxon for a species label."""
    name = (species or "Unlabeled").strip() or "Unlabeled"
    taxon = db.session.execute(
        select(Taxon).where(Taxon.scientific_name == name)
    ).scalar_one_or_none()
    if taxon is None:
        taxon = Taxon(
            scientific_name=name,
            rank="unclassified" if name.lower() == "unlabeled" else rank,
        )
        db.session.add(taxon)
        db.session.flush()
    return taxon


def start_model_run(sampling_metadata_id, parameters=None, model_version=None):
    """Create a running model-execution record for the selected model version."""
    run = ModelRun(
        model_version=model_version,
        sampling_metadata_id=sampling_metadata_id,
        status="running",
        parameters_json=json.dumps(parameters) if parameters else None,
    )
    db.session.add(run)
    db.session.flush()
    return run


def record_prediction(sample, model_run=None, source="model"):
    """Normalize a sample's current label and append prediction history."""
    taxon = get_or_create_taxon(sample.species)
    sample.taxon = taxon
    sample.model_run = model_run
    sample.review_status = "unreviewed"
    prediction = Prediction(
        sample=sample,
        taxon=taxon,
        model_run=model_run,
        confidence=sample.confidence,
        source=source,
    )
    db.session.add(prediction)
    return prediction


def record_review(sample, species, reviewer_id=None, notes=None):
    """Apply a human label to a sample and append immutable review history."""
    previous_species = sample.species
    taxon = get_or_create_taxon(species)
    accepted = previous_species == taxon.scientific_name
    sample.species = taxon.scientific_name
    sample.taxon = taxon
    sample.review_status = "reviewed"
    if not accepted:
        sample.confidence = 1.0
        sample.model_run = None
    review = Review(
        sample=sample,
        reviewer_id=reviewer_id,
        taxon=taxon,
        previous_species=previous_species,
        status="accepted" if accepted else "corrected",
        notes=notes,
    )
    db.session.add(review)
    return review


### --- SCHEMA INTROSPECTION UTILITIES --- ###


def get_tables():
    """Returns a list of all table names in the current metadata."""
    return list(db.Model.metadata.tables.keys())


def get_columns(table_name):
    """
    Retrieves detailed column information for a specific table.
    :param table_name: String name of the table.
    """
    table = db.Model.metadata.tables[table_name]
    return [
        {"name": col.name, "type": str(col.type), "primary_key": col.primary_key}
        for col in table.columns
    ]


def get_relationships():
    """
    Introspects foreign keys to determine relationships between tables.
    :return: List of relationship dictionaries (from_table to to_table).
    """
    relationships = []
    for table_name, table in db.Model.metadata.tables.items():
        for fk in table.foreign_keys:
            relationships.append(
                {
                    "from_table": table_name,
                    "from_column": fk.parent.name,
                    "to_table": fk.column.table.name,
                    "to_column": fk.column.name,
                }
            )
    return relationships


def build_join_graph():
    """Constructs an adjacency list of table relationships for graph traversal."""
    graph = {}
    for rel in get_relationships():
        graph.setdefault(rel["from_table"], []).append(rel["to_table"])
    return graph


def get_table_columns():
    """Returns a map of all tables/reporting views and their column metadata."""
    data = {}
    for name, model in classes.items():
        data[name] = [
            {"name": col.name, "type": str(col.type)} for col in model.__table__.columns
        ]
    schema_inspector = inspect(db.engine)
    available_views = set(schema_inspector.get_view_names())
    for view in REPORTING_VIEWS:
        if view in available_views:
            data[view] = [
                {"name": column["name"], "type": str(column["type"]), "view": True}
                for column in schema_inspector.get_columns(view)
            ]
    return data


### --- DYNAMIC QUERY BUILDER LOGIC --- ###

# Maps string operators from the frontend to SQLAlchemy comparison functions
OPERATOR_MAP = {
    "=": lambda c, v: c == v,
    "!=": lambda c, v: c != v,
    ">": lambda c, v: c > v,
    "<": lambda c, v: c < v,
    ">=": lambda c, v: c >= v,
    "<=": lambda c, v: c <= v,
    "LIKE": lambda c, v: c.like(v),
}


def build_select(payload):
    """
    Constructs the initial SELECT statement including specific columns or wildcard.
    :param payload: Dict containing 'base_table' and 'columns' to select.
    """
    base_model = classes[payload["base_table"]]
    select_items = []

    for table_name, cols in payload["columns"].items():
        model = classes[table_name]
        if not cols:
            select_items.extend(model.__table__.columns)
        else:
            for col in cols:
                select_items.append(getattr(model, col))

    return select(*select_items).select_from(base_model)


def apply_joins(stmt, payload):
    """
    Applies JOIN clauses to a SQLAlchemy statement based on payload definitions.
    """
    for join in payload["joins"]:
        left_model = classes[join["from_table"]]
        right_model = classes[join["to_table"]]
        stmt = stmt.join(
            right_model,
            getattr(left_model, join["from_column"])
            == getattr(right_model, join["to_column"]),
        )
    return stmt


def build_where(payload):
    """
    Recursively builds complex WHERE clauses with support for nested parentheses.
    :param payload: Dict containing 'conditions' with logical operators and nesting.
    """
    stack = []
    current = []

    for cond in payload["conditions"]:
        model = classes[cond["table"]]
        column = getattr(model, cond["column"])
        expr = OPERATOR_MAP[cond["operator"]](column, cond["value"])

        # Handle nesting logic
        for _ in range(cond.get("openParen", 0)):
            stack.append((current, cond.get("logic", "AND")))
            current = []

        current.append(expr)

        for _ in range(cond.get("closeParen", 0)):
            grouped = and_(*current)
            current, _ = stack.pop()
            current.append(grouped)

    return and_(*current) if current else None


def build_query(payload):
    """
    High-level entry point to assemble a full SQLAlchemy query from a JSON payload.
    """
    stmt = build_select(payload)
    stmt = apply_joins(stmt, payload)
    where_clause = build_where(payload)
    if where_clause is not None:
        stmt = stmt.where(where_clause)
    return stmt


### --- EXPORT UTILITIES --- ###


TRAINING_AUGMENTATIONS = frozenset(
    {
        "rotate",
        "horizontal_flip",
        "vertical_flip",
        "brightness_contrast",
        "zoom",
        "noise",
        "blur",
    }
)


class TrainingExportError(ValueError):
    """Raised when training-set export options or source images are invalid."""


def get_training_species_counts():
    """Return reviewed and total sample counts for each trainable species."""
    rows = db.session.execute(
        select(Samples.species, Samples.review_status, func.count(Samples.id))
        .where(func.lower(func.trim(Samples.species)) != "unlabeled")
        .group_by(Samples.species, Samples.review_status)
        .order_by(Samples.species)
    ).all()
    counts = defaultdict(lambda: {"reviewed": 0, "total": 0})
    for species, review_status, count in rows:
        counts[species]["total"] += count
        if review_status == "reviewed":
            counts[species]["reviewed"] += count
    return [
        {"species": species, **values}
        for species, values in sorted(counts.items(), key=lambda item: item[0].lower())
    ]


def normalize_training_export_options(options=None):
    """Validate and normalize the configurable training-set export contract."""
    options = options or {}
    review_scope = options.get("review_scope", "reviewed")
    if review_scope not in {"reviewed", "all"}:
        raise TrainingExportError("Review scope must be 'reviewed' or 'all'.")

    distribution = options.get("distribution", "natural")
    if distribution not in {"natural", "balanced", "custom"}:
        raise TrainingExportError(
            "Distribution must be natural, balanced, or custom."
        )

    available_counts = get_training_species_counts()
    count_key = "reviewed" if review_scope == "reviewed" else "total"
    available_species = {
        item["species"] for item in available_counts if item[count_key] > 0
    }
    requested_species = options.get("species") or sorted(available_species)
    if isinstance(requested_species, str):
        requested_species = [requested_species]
    selected_species = list(dict.fromkeys(str(name) for name in requested_species))
    unknown_species = set(selected_species) - available_species
    if unknown_species:
        raise TrainingExportError(
            "Selected species have no eligible samples: "
            + ", ".join(sorted(unknown_species))
        )
    if not selected_species:
        raise TrainingExportError("Select at least one species to export.")

    try:
        validation_percent = int(options.get("validation_percent", 20))
        seed = int(options.get("seed", 42))
        target_per_species = int(options.get("target_per_species", 0) or 0)
        augmentation_copies = int(options.get("augmentation_copies", 0) or 0)
    except (TypeError, ValueError) as error:
        raise TrainingExportError(
            "Validation percentage, seed, targets, and copies must be integers."
        ) from error

    if not 1 <= validation_percent <= 40:
        raise TrainingExportError("Validation percentage must be between 1 and 40.")
    if not 0 <= seed <= 4_294_967_295:
        raise TrainingExportError("Seed must be between 0 and 4294967295.")
    if not 0 <= augmentation_copies <= 10:
        raise TrainingExportError("Augmented copies must be between 0 and 10.")

    requested_augmentations = options.get("augmentations") or []
    if isinstance(requested_augmentations, str):
        requested_augmentations = [requested_augmentations]
    augmentations = list(dict.fromkeys(requested_augmentations))
    unknown_augmentations = set(augmentations) - TRAINING_AUGMENTATIONS
    if unknown_augmentations:
        raise TrainingExportError(
            "Unknown augmentation options: "
            + ", ".join(sorted(unknown_augmentations))
        )
    if distribution != "natural":
        augmentation_copies = 0
    if augmentation_copies and not augmentations:
        raise TrainingExportError(
            "Select at least one augmentation when requesting augmented copies."
        )

    raw_targets = options.get("species_targets") or {}
    if not isinstance(raw_targets, dict):
        raise TrainingExportError("Species targets must be a name-to-count mapping.")
    species_targets = {}
    if distribution == "balanced":
        if not 1 <= target_per_species <= 10_000:
            raise TrainingExportError(
                "Balanced exports require 1 to 10000 base samples per species."
            )
        species_targets = {
            species: target_per_species for species in selected_species
        }
    elif distribution == "custom":
        for species in selected_species:
            try:
                target = int(raw_targets.get(species, 0))
            except (TypeError, ValueError) as error:
                raise TrainingExportError(
                    f"The target for {species} must be an integer."
                ) from error
            if not 1 <= target <= 10_000:
                raise TrainingExportError(
                    f"The target for {species} must be between 1 and 10000."
                )
            species_targets[species] = target

    return {
        "review_scope": review_scope,
        "species": selected_species,
        "distribution": distribution,
        "target_per_species": target_per_species,
        "species_targets": species_targets,
        "validation_percent": validation_percent,
        "seed": seed,
        "augmentations": augmentations,
        "augmentation_copies": augmentation_copies,
    }


def _split_training_samples(samples, validation_fraction, rng):
    """Split samples by source image where possible to reduce validation leakage."""
    if len(samples) < 2:
        return list(samples), [], "Only one sample was available; no validation item was created."

    groups = defaultdict(list)
    for sample in samples:
        groups[sample["sampling_metadata_id"]].append(sample)
    group_ids = list(groups)
    rng.shuffle(group_ids)
    validation_goal = max(1, round(len(samples) * validation_fraction))

    if len(group_ids) > 1:
        validation_ids = set()
        validation_count = 0
        for group_id in group_ids[:-1]:
            validation_ids.add(group_id)
            validation_count += len(groups[group_id])
            if validation_count >= validation_goal:
                break
        training = [
            sample
            for group_id in group_ids
            if group_id not in validation_ids
            for sample in groups[group_id]
        ]
        validation = [
            sample
            for group_id in group_ids
            if group_id in validation_ids
            for sample in groups[group_id]
        ]
        return training, validation, None

    shuffled = list(samples)
    rng.shuffle(shuffled)
    validation_count = min(len(shuffled) - 1, validation_goal)
    return (
        shuffled[validation_count:],
        shuffled[:validation_count],
        "Only one source image was available; validation was split by crop.",
    )


def _species_folder_name(species):
    """Return a ZIP-safe folder name for a species label."""
    folder = re.sub(r"[^A-Za-z0-9._-]+", "_", species).strip("._")
    return folder or "unclassified"


def _decode_training_image(sample):
    """Decode one stored base64 crop and normalize it to a Pillow image."""
    try:
        raw = b64decode(sample["image_base64"], validate=True)
        image = Image.open(io.BytesIO(raw))
        image.load()
    except Exception as error:
        raise TrainingExportError(
            f"Sample {sample['id']} does not contain a valid encoded image."
        ) from error
    if image.mode not in {"L", "RGB"}:
        image = image.convert("RGB")
    return image


def _image_as_png(image):
    """Encode a Pillow image as PNG bytes."""
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()


def _augment_training_image(image, augmentations, rng):
    """Apply a deterministic random combination of selected augmentations."""
    enabled = list(augmentations)
    selected = [name for name in enabled if rng.random() < 0.65]
    if enabled and not selected:
        selected = [rng.choice(enabled)]

    transformed = image.copy()
    applied = []
    for name in selected:
        if name == "rotate":
            angle = rng.choice((90, 180, 270))
            transformed = transformed.rotate(angle, expand=True)
            applied.append(f"rotate_{angle}")
        elif name == "horizontal_flip":
            transformed = ImageOps.mirror(transformed)
            applied.append(name)
        elif name == "vertical_flip":
            transformed = ImageOps.flip(transformed)
            applied.append(name)
        elif name == "brightness_contrast":
            brightness = round(rng.uniform(0.82, 1.18), 3)
            contrast = round(rng.uniform(0.82, 1.18), 3)
            transformed = ImageEnhance.Brightness(transformed).enhance(brightness)
            transformed = ImageEnhance.Contrast(transformed).enhance(contrast)
            applied.append(f"brightness_{brightness}")
            applied.append(f"contrast_{contrast}")
        elif name == "zoom":
            width, height = transformed.size
            crop_ratio = rng.uniform(0.04, 0.14)
            crop_x = min(max(1, int(width * crop_ratio)), max(1, width // 3))
            crop_y = min(max(1, int(height * crop_ratio)), max(1, height // 3))
            if width > crop_x * 2 and height > crop_y * 2:
                transformed = transformed.crop(
                    (crop_x, crop_y, width - crop_x, height - crop_y)
                ).resize((width, height), Image.Resampling.BICUBIC)
                applied.append(f"zoom_{round(1 / (1 - 2 * crop_ratio), 3)}")
        elif name == "noise":
            array = np.asarray(transformed).astype(np.float32)
            noise_rng = np.random.default_rng(rng.randrange(0, 2**32))
            sigma = round(rng.uniform(2.0, 8.0), 3)
            array = np.clip(
                array + noise_rng.normal(0, sigma, array.shape), 0, 255
            ).astype(np.uint8)
            transformed = Image.fromarray(array, mode=transformed.mode)
            applied.append(f"noise_{sigma}")
        elif name == "blur":
            radius = round(rng.uniform(0.3, 0.9), 3)
            transformed = transformed.filter(ImageFilter.GaussianBlur(radius))
            applied.append(f"blur_{radius}")
    return transformed, applied


def build_species_zip(options=None):
    """Build a filtered, split-safe, reproducible training-set ZIP archive."""
    normalized = normalize_training_export_options(options)
    conditions = [Samples.species.in_(normalized["species"])]
    if normalized["review_scope"] == "reviewed":
        conditions.append(Samples.review_status == "reviewed")
    samples = db.session.execute(
        select(
            Samples.id,
            Samples.sampling_metadata_id,
            Samples.species,
            Samples.image_base64,
            Samples.review_status,
        ).where(*conditions)
    ).mappings().all()
    grouped_samples = defaultdict(list)
    for sample in samples:
        grouped_samples[sample["species"]].append(dict(sample))

    manifest = {
        "format_version": 1,
        "created_at": utc_now().isoformat(),
        "options": normalized,
        "warnings": [],
        "species": {},
        "files": [],
    }
    zip_buffer = io.BytesIO()
    used_folders = {}

    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zipf:
        for species in normalized["species"]:
            species_samples = grouped_samples[species]
            species_seed = normalized["seed"] + int(
                hashlib.sha256(species.encode("utf-8")).hexdigest()[:8], 16
            )
            rng = random.Random(species_seed)
            rng.shuffle(species_samples)

            target = normalized["species_targets"].get(
                species, len(species_samples)
            )
            selected_originals = species_samples[: min(target, len(species_samples))]
            training, validation, split_warning = _split_training_samples(
                selected_originals,
                normalized["validation_percent"] / 100,
                rng,
            )
            if split_warning:
                manifest["warnings"].append(f"{species}: {split_warning}")

            folder = _species_folder_name(species)
            if folder in used_folders and used_folders[folder] != species:
                folder += "_" + hashlib.sha256(species.encode("utf-8")).hexdigest()[:8]
            used_folders[folder] = species

            species_manifest = {
                "folder": folder,
                "eligible": len(species_samples),
                "original_train": 0,
                "original_valid": 0,
                "augmented_train": 0,
            }
            decoded_training = []
            for split, split_samples in (("train", training), ("valid", validation)):
                for sample in split_samples:
                    image = _decode_training_image(sample)
                    filename = f"{split}/{folder}/sample_{sample['id']}.png"
                    zipf.writestr(filename, _image_as_png(image))
                    manifest["files"].append(
                        {
                            "path": filename,
                            "sample_id": sample["id"],
                            "sampling_metadata_id": sample["sampling_metadata_id"],
                            "species": species,
                            "review_status": sample["review_status"],
                            "augmented": False,
                            "transformations": [],
                        }
                    )
                    species_manifest[f"original_{split}"] += 1
                    if split == "train":
                        decoded_training.append((sample, image))

            shortage = max(0, target - len(selected_originals))
            augmentation_total = shortage + (
                len(decoded_training) * normalized["augmentation_copies"]
            )
            if augmentation_total and not normalized["augmentations"]:
                manifest["warnings"].append(
                    f"{species}: requested target exceeds available samples, but no "
                    "augmentation was selected."
                )
                augmentation_total = 0
            if augmentation_total and not decoded_training:
                manifest["warnings"].append(
                    f"{species}: no training image was available for augmentation."
                )
                augmentation_total = 0

            for index in range(augmentation_total):
                source_sample, source_image = decoded_training[
                    index % len(decoded_training)
                ]
                augmented, transformations = _augment_training_image(
                    source_image,
                    normalized["augmentations"],
                    rng,
                )
                filename = (
                    f"train/{folder}/aug_{source_sample['id']}_{index + 1}.png"
                )
                zipf.writestr(filename, _image_as_png(augmented))
                manifest["files"].append(
                    {
                        "path": filename,
                        "sample_id": source_sample["id"],
                        "sampling_metadata_id": source_sample[
                            "sampling_metadata_id"
                        ],
                        "species": species,
                        "review_status": source_sample["review_status"],
                        "augmented": True,
                        "transformations": transformations,
                    }
                )
                species_manifest["augmented_train"] += 1

            manifest["species"][species] = species_manifest

        zipf.writestr(
            "manifest.json",
            json.dumps(manifest, indent=2, sort_keys=True),
        )

    zip_buffer.seek(0)
    return zip_buffer
