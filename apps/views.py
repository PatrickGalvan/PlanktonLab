"""Flask page and JSON endpoint implementations.

Route request fields, response formats, side effects, and curl examples are listed in
``docs/API_REFERENCE.md``.
"""

# Flask modules
from flask import (
    flash,
    request,
    redirect,
    url_for,
    render_template,
    jsonify,
    abort,
    Response,
    send_file,
    session,
    stream_with_context,
)
from werkzeug.utils import secure_filename
from markdown_it import MarkdownIt
from markupsafe import Markup
from datetime import date, datetime
from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    Integer,
    Text,
    and_,
    delete,
    func,
    insert,
    select,
)
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
import hashlib
import tempfile

import os
from apps.static.assets.utils import image_class
from apps.static.assets.utils.countries import country_choices
from apps.static.assets.utils.database_classes import (
    Lakes,
    ModelVersion,
    ReadOnlyQueryError,
    Samples,
    Sampling_metadata,
    Taxon,
    TrainingExportError,
    User,
    build_join_graph,
    build_query,
    build_species_zip,
    classes,
    db,
    execute_read_only_query,
    get_relationships,
    get_table_columns,
    get_tables,
    get_training_species_counts,
    record_prediction,
    record_review,
    start_model_run,
    utc_now,
)
from apps.static.assets.utils.lake_metadata import (
    LakeLookupUnavailable,
    search_lakes,
)
import json
from pathlib import Path

# App modules
from apps import app

# initiate Image class
tmp_image = image_class.Image_Classifier()

PROJECT_ROOT = Path(app.root_path).parent.resolve()
_model_learner_cache = {}


class ModelArtifactError(RuntimeError):
    """Raised when a stored classifier artifact cannot be safely loaded."""


def model_storage_directory():
    """Return the configured, non-public directory for versioned model artifacts."""
    configured = Path(app.config["MODEL_STORAGE_FOLDER"])
    if not configured.is_absolute():
        configured = PROJECT_ROOT / configured
    return configured.resolve()


def resolve_model_artifact(model_version):
    """Resolve and validate a model artifact path against approved directories."""
    if not model_version or not model_version.artifact_path:
        raise ModelArtifactError("This model does not have an artifact file.")

    artifact_path = Path(model_version.artifact_path)
    if not artifact_path.is_absolute():
        artifact_path = PROJECT_ROOT / artifact_path
    artifact_path = artifact_path.resolve()

    storage_directory = model_storage_directory()
    try:
        artifact_path.relative_to(storage_directory)
    except ValueError as error:
        raise ModelArtifactError("The model artifact path is outside model storage.") from error
    if not artifact_path.is_file():
        raise ModelArtifactError("The model artifact file is missing.")
    return artifact_path


def load_model_version(model_version):
    """Load and cache an immutable model artifact after validating its path."""
    artifact_path = resolve_model_artifact(model_version)
    cache_key = (model_version.id, str(artifact_path), artifact_path.stat().st_mtime_ns)
    learner = _model_learner_cache.get(cache_key)
    if learner is None:
        try:
            learner = tmp_image.load_learner_from_file(
                artifact_path,
                raise_errors=True,
            )
        except Exception as error:
            app.logger.warning(
                "Could not load model version %s from %s",
                model_version.id,
                artifact_path,
                exc_info=True,
            )
            raise ModelArtifactError("The selected model could not be loaded.") from error
        _model_learner_cache.clear()
        _model_learner_cache[cache_key] = learner
    return learner


def selected_model_version():
    """Return this browser session's model, falling back to the global default."""
    selected_id = session.get("selected_model_version_id")
    selected_model = db.session.get(ModelVersion, selected_id) if selected_id else None
    if selected_id and selected_model is None:
        session.pop("selected_model_version_id", None)
    if selected_model is not None:
        return selected_model
    return db.session.execute(
        select(ModelVersion)
        .where(ModelVersion.is_active.is_(True))
        .order_by(ModelVersion.created_at.desc())
    ).scalars().first()


def available_model_versions():
    """Return model versions newest first for runtime selectors."""
    return db.session.execute(
        select(ModelVersion).order_by(ModelVersion.created_at.desc(), ModelVersion.id.desc())
    ).scalars().all()


def model_redirect_target(default_endpoint="training_page"):
    """Allow local post-action redirects without accepting external URLs."""
    target = request.form.get("next", "")
    if target.startswith("/") and not target.startswith("//"):
        return target
    return url_for(default_endpoint)


### --- ROUTING & LOGIC --- ###


MANAGED_DATABASE_FIELDS = frozenset({"id", "created_at", "updated_at"})
LAKE_FIELD_LABELS = {
    "depth": "Maximum depth (m)",
    "area": "Surface area (km²)",
    "latitude": "Latitude",
    "longitude": "Longitude",
    "elevation_m": "Elevation (m)",
    "mean_depth_m": "Mean depth (m)",
    "volume_km3": "Volume (km³)",
    "shoreline_length_km": "Shoreline length (km)",
    "catchment_area_km2": "Catchment area (km²)",
    "residence_time_days": "Residence time (days)",
    "waterbody_type": "Waterbody type",
    "administrative_region": "Administrative region",
}
LAKE_PROVENANCE_FIELDS = frozenset(
    {"external_source", "external_id", "source_url", "metadata_retrieved_at"}
)


def editable_database_columns(table):
    """Return user-editable columns while excluding IDs and audit timestamps."""
    return [
        column
        for column in db.metadata.tables[table].columns
        if column.name not in MANAGED_DATABASE_FIELDS
    ]


def serialize_form_value(value):
    """Convert a database value to an HTML form-compatible string."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, datetime):
        return value.replace(tzinfo=None).isoformat(timespec="minutes")
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def database_form_fields(table, record=None, submitted=None, editing=False):
    """Build typed field metadata for the generic add/edit form."""
    fields = []
    for column in editable_database_columns(table):
        if submitted is not None and column.name in submitted:
            value = submitted.get(column.name, "")
        elif record is not None:
            value = serialize_form_value(getattr(record, column.name))
        else:
            value = ""

        input_type = "text"
        step = None
        multiline = isinstance(column.type, Text)
        if isinstance(column.type, Boolean):
            input_type = "boolean"
        elif isinstance(column.type, DateTime):
            input_type = "datetime-local"
        elif isinstance(column.type, Date):
            input_type = "date"
        elif isinstance(column.type, Integer):
            input_type = "number"
            step = "1"
        elif isinstance(column.type, Float):
            input_type = "number"
            step = "any"
        elif column.name == "email":
            input_type = "email"
        elif table == "lakes" and column.name == "country":
            input_type = "select"
        if table == "lakes" and column.name in LAKE_PROVENANCE_FIELDS:
            input_type = "hidden"

        required = not column.nullable and (
            editing or (column.default is None and column.server_default is None)
        )
        fields.append(
            {
                "name": column.name,
                "label": LAKE_FIELD_LABELS.get(
                    column.name, column.name.replace("_", " ").title()
                ),
                "input_type": input_type,
                "step": step,
                "multiline": multiline,
                "required": required,
                "nullable": column.nullable,
                "value": value,
                "options": country_choices(value) if input_type == "select" else (),
            }
        )
    return fields


def database_form_values(table, form, editing=False):
    """Coerce allow-listed form values to their SQLAlchemy column types."""
    values = {}
    for column in editable_database_columns(table):
        raw_value = form.get(column.name)
        label = column.name.replace("_", " ").title()

        if raw_value is None or not raw_value.strip():
            if not editing and (column.default is not None or column.server_default is not None):
                continue
            if column.nullable:
                values[column.name] = None
                continue
            raise ValueError(f"{label} is required.")

        value = raw_value.strip()
        try:
            if isinstance(column.type, Boolean):
                normalized = value.lower()
                if normalized not in {"true", "false", "1", "0"}:
                    raise ValueError
                value = normalized in {"true", "1"}
            elif isinstance(column.type, DateTime):
                value = datetime.fromisoformat(value)
            elif isinstance(column.type, Date):
                value = date.fromisoformat(value)
            elif isinstance(column.type, Integer):
                value = int(value)
            elif isinstance(column.type, Float):
                value = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"Enter a valid value for {label}.") from None

        values[column.name] = value
    if table == "lakes":
        validate_lake_values(values)
    return values


def validate_lake_values(values):
    """Validate optional lake measurements and geographic coordinates."""
    for field in (
        "depth",
        "area",
        "elevation_m",
        "mean_depth_m",
        "volume_km3",
        "shoreline_length_km",
        "catchment_area_km2",
        "residence_time_days",
    ):
        value = values.get(field)
        if value is not None and value < 0:
            raise ValueError(f"{LAKE_FIELD_LABELS[field]} cannot be negative.")
    latitude = values.get("latitude")
    longitude = values.get("longitude")
    if latitude is not None and not -90 <= latitude <= 90:
        raise ValueError("Latitude must be between -90 and 90.")
    if longitude is not None and not -180 <= longitude <= 180:
        raise ValueError("Longitude must be between -180 and 180.")


def onboarding_requirements():
    """Check which first-run records classification still requires.

    :return: Mapping with ``user`` and ``lake`` flags; true means the record is
        missing.
    """
    return {
        "user": db.session.execute(select(User.id).limit(1)).scalar_one_or_none()
        is None,
        "lake": db.session.execute(select(Lakes.id).limit(1)).scalar_one_or_none()
        is None,
    }


@app.route("/", defaults={"path": "index.html"})
@app.route("/")
def index():
    """
    Render the landing page or start onboarding for an unconfigured database.

    :return: Rendered landing page, or a redirect to the setup route.
    """
    if any(onboarding_requirements().values()):
        return redirect(url_for("setup"))
    return render_template("/pages/landing_page.html")


@app.route("/setup", methods=["GET", "POST"])
def setup():
    """Create the first operator and lake required by classification.

    On GET, render fields for only the missing record types. On POST, validate and
    create those records atomically. Configured databases skip directly to source
    selection.

    :param username: (Form) Required operator name when no user exists.
    :param email: (Form) Required operator email when no user exists.
    :param lake/country: (Form) Required location values when no lake exists.
    :param depth/area: (Form) Optional non-negative lake measurements.
    :return: Setup page after GET or invalid input; otherwise a redirect to source
        selection.
    """
    requirements = onboarding_requirements()
    if not any(requirements.values()):
        return redirect(url_for("select_source"))

    if request.method == "POST":
        try:
            if requirements["user"]:
                username = request.form.get("username", "").strip()
                email = request.form.get("email", "").strip()
                if not username or not email or "@" not in email:
                    raise ValueError("Enter a name and a valid email address.")
                db.session.add(User(username=username, email=email))

            if requirements["lake"]:
                lake_values = database_form_values("lakes", request.form)
                db.session.add(Lakes(**lake_values))

            db.session.commit()
        except (IntegrityError, ValueError) as error:
            db.session.rollback()
            message = (
                str(error)
                if isinstance(error, ValueError)
                else "That user or lake already exists. Choose a different value."
            )
            flash(message, "error")
        else:
            flash("Setup complete. You can now choose an image to classify.", "success")
            return redirect(url_for("select_source"))

    return render_template(
        "pages/setup.html",
        needs_user=requirements["user"],
        needs_lake=requirements["lake"],
        countries=country_choices(request.form.get("country")),
    )


@app.route("/api/lakes/search", methods=["GET"])
def lake_metadata_search():
    """Find lake candidates on Wikidata without blocking manual entry."""
    name = request.args.get("name", "").strip()
    country = request.args.get("country", "").strip()
    try:
        results = search_lakes(name, country=country or None)
    except ValueError as error:
        return jsonify({"error": str(error), "manual_entry": True}), 400
    except LakeLookupUnavailable as error:
        app.logger.info("Wikidata lake lookup unavailable: %s", error)
        return jsonify({"error": str(error), "manual_entry": True}), 503
    return jsonify({"results": results, "manual_entry": True})


@app.route("/docs", methods=["GET"])
def documentation():
    """Render the checked-in API reference as an HTML documentation page.

    The Markdown source is maintained in ``docs/API_REFERENCE.md``. Raw HTML is
    disabled during conversion, and the resulting renderer output is explicitly
    marked safe for insertion into the page template.

    Returns:
        flask.Response: Rendered documentation page.

    Example:
        Open ``http://127.0.0.1:5000/docs`` while the application is running.
    """
    documentation_path = Path(app.root_path).parent / "docs" / "API_REFERENCE.md"
    if not documentation_path.is_file():
        abort(404)

    markdown = MarkdownIt("commonmark", {"html": False}).enable("table")
    documentation_html = Markup(
        markdown.render(documentation_path.read_text(encoding="utf-8"))
    )
    return render_template(
        "pages/documentation.html", documentation_html=documentation_html
    )


@app.route("/pages/select_image_source", methods=["GET", "POST"])
def select_source():
    """
    Handles the selection of image sources, allowing users to load from the
    database or upload and process a new image.

    :param action: (Form) Logic switch to either 'load_from_db' or 'classify_new_image'.
    :param db_selection: (Form) ID of existing image if loading from DB.
    :param image_file: (File) The raw image file for new classification.
    :param user_selection/lake_selection/date_selection: (Form) Metadata for new entries.
    :return: Redirects to classification view or renders the selection form.
    """
    if any(onboarding_requirements().values()):
        return redirect(url_for("setup"))

    if request.method == "POST":
        action = request.form["action"]  # Get the selected action
        if action == "load_from_db":
            return redirect(
                url_for("load_from_db", image_id=request.form["db_selection"])
            )  # Redirect to route for loading image from DB
        elif action == "classify_new_image":
            if "image_file" in request.files:
                file = request.files["image_file"]

                if file.filename == "":
                    flash("No selected file")
                    return redirect(request.url)
                if file:
                    upload_name = secure_filename(file.filename)
                    if not upload_name:
                        flash("The selected file name is not valid.", "error")
                        return redirect(request.url)
                    file_with_path = os.path.join(
                        app.config["UPLOAD_FOLDER"],
                        upload_name,
                    )
                    file.save(file_with_path)
                    tmp_image.read_image(file_with_path)
                    tmp_image.create_mask()
                    tmp_image.draw_boxes()

                    # date conversion from form
                    date_str = request.form["date_selection"]
                    date_obj = datetime.strptime(date_str, "%Y-%m-%d").date()

                    # insert metadata into db
                    sampling_meta = {
                        "user_id": request.form["user_selection"],
                        "lake_id": request.form["lake_selection"],
                        "image_name": upload_name,
                        "sampling_date": date_obj,
                        "image_path": file_with_path,
                    }
                    stmt = insert(Sampling_metadata).values(sampling_meta)
                    db.session.execute(stmt)
                    db.session.commit()

                    stmt = select(Sampling_metadata.id).where(
                        Sampling_metadata.image_name == upload_name
                    )

                    image_id = db.session.execute(stmt).scalar()

                    return redirect(url_for("load_from_db", image_id=image_id))
            else:
                return "No file uploaded", 400
    return render_template(
        "pages/select_source_form.html",
        users=User.query.all(),
        lakes=Lakes.query.all(),
        db_files=Sampling_metadata.query.all(),
    )


def first_review_species(image_id):
    """Return the first unreviewed taxon, or the first available taxon."""
    base_query = select(Samples.species).where(
        Samples.sampling_metadata_id == image_id,
        Samples.species.is_not(None),
        Samples.species != "",
    )
    review_species = db.session.execute(
        base_query.where(Samples.review_status != "reviewed")
        .group_by(Samples.species)
        .order_by(func.lower(Samples.species), Samples.species)
        .limit(1)
    ).scalar_one_or_none()
    if review_species:
        return review_species

    return db.session.execute(
        base_query.group_by(Samples.species)
        .order_by(func.lower(Samples.species), Samples.species)
        .limit(1)
    ).scalar_one_or_none()


@app.route("/pages/classification/<int:image_id>", methods=["GET"])
def load_from_db(image_id):
    """
    Loads image data and existing sample classifications from the database for display.

    :param image_id: (URL) The unique identifier for the Sampling_metadata record.
    :return: Rendered classification page with image masks and bounding box data.
    """
    if any(onboarding_requirements().values()):
        return redirect(url_for("setup"))

    if request.method == "GET":
        image_path = db.session.get(Sampling_metadata, image_id)
        tmp_image.meta_id = image_id
        tmp_image.read_image(image_path.image_path)
        tmp_image.create_mask()
        stmt = select(
            Samples.x,
            Samples.y,
            Samples.w,
            Samples.h,
            Samples.species,
            Samples.confidence,
            Samples.id,
        ).where(Samples.sampling_metadata_id == image_id)

        rows = db.session.execute(stmt).all()
        tmp_image.contour_array = [list(row) for row in rows]

        models = available_model_versions()
        selected_model = selected_model_version()
        return render_template(
            "pages/classification.html",
            img_data_mask=tmp_image.mask_io.decode("utf-8"),
            img_data_raw=tmp_image.curr_image_io.decode("utf-8"),
            img_boxes=json.dumps(tmp_image.contour_array or []),
            model_versions=models,
            selected_model=selected_model,
            image_id=image_id,
            review_species=first_review_species(image_id),
        )


@app.route("/pages/database")
def pages_database():
    """
    Displays a list of all available tables in the database.

    :return: Rendered HTML template for the database overview.
    """
    tables = db.metadata.tables.keys()
    return render_template("pages/database.html", tables=tables)


@app.route("/table/<param>", methods=["GET"])
def tables_builder(param):
    """
    Builds a paginated view of a specific database table.

    :param param: (URL) The name of the table to display.
    :param page: (Query) The current page number for pagination.
    :return: Rendered HTML table with headers and paginated data.
    """
    if param not in classes:
        abort(404)

    page = request.args.get("page", 1, type=int)
    per_page = 10

    table_headers = list(db.metadata.tables[param].columns.keys())
    pagination = classes[param].query.paginate(
        page=page, per_page=per_page, error_out=False
    )

    return render_template(
        "pages/table.html",
        table_headers=table_headers,
        table=param,
        table_data=pagination.items,
        pagination=pagination,
    )


@app.route("/add_data/<param>")
def add_data(param):
    """
    Displays a dynamic input form for adding a new entry to a specific table.

    :param param: (URL) The name of the table receiving data.
    :return: Rendered entry form with fields derived from table columns.
    """
    if param not in classes:
        abort(404)
    return render_template(
        "pages/add_db_entry.html",
        fields=database_form_fields(param),
        table=param,
        mode="add",
    )


@app.route("/add", methods=["POST", "GET"])
def insert_do_db():
    """
    Processes form submission to insert a new record into the database.

    :param table: (Form) The target table name.
    :param result_dict: (Form) Key-value pairs matching table columns.
    :return: Redirects to the table view on POST, or database home on GET.
    """
    if request.method == "POST":
        table = request.form.get("table")
        if table not in classes:
            abort(404)

        try:
            values = database_form_values(table, request.form)
            record = classes[table](**values)
            db.session.add(record)
            db.session.commit()
        except ValueError as error:
            db.session.rollback()
            flash(str(error), "error")
        except IntegrityError:
            db.session.rollback()
            flash(
                "The record could not be added because a unique or referenced "
                "value is invalid.",
                "error",
            )
        except SQLAlchemyError:
            db.session.rollback()
            app.logger.exception("Database error adding table=%s", table)
            if app.debug:
                raise
            flash("The record could not be added. No changes were saved.", "error")
        else:
            flash("The record was added.", "success")
            return redirect(url_for("tables_builder", param=table))

        return render_template(
            "pages/add_db_entry.html",
            fields=database_form_fields(table, submitted=request.form),
            table=table,
            mode="add",
        )
    else:
        return redirect(url_for("pages_database"))


@app.route("/edit/<table>/<int:id>", methods=["GET", "POST"])
def edit_database_row(table, id):
    """Edit one row while keeping IDs and audit timestamps server-managed."""
    if table not in classes:
        abort(404)

    record = db.session.get(classes[table], id)
    if record is None:
        abort(404)

    if request.method == "POST":
        try:
            values = database_form_values(table, request.form, editing=True)
            for field, value in values.items():
                setattr(record, field, value)
            if hasattr(record, "updated_at"):
                record.updated_at = utc_now()
            db.session.commit()
        except ValueError as error:
            db.session.rollback()
            flash(str(error), "error")
        except IntegrityError:
            db.session.rollback()
            flash(
                "The row could not be updated because a unique or referenced "
                "value is invalid.",
                "error",
            )
        except SQLAlchemyError:
            db.session.rollback()
            app.logger.exception("Database error updating table=%s id=%s", table, id)
            if app.debug:
                raise
            flash("The row could not be updated. No changes were saved.", "error")
        else:
            flash("The row was updated.", "success")
            return redirect(url_for("tables_builder", param=table))

    return render_template(
        "pages/add_db_entry.html",
        fields=database_form_fields(
            table,
            record=record,
            submitted=request.form if request.method == "POST" else None,
            editing=True,
        ),
        table=table,
        mode="edit",
        record_id=id,
    )


@app.route("/delete/<table>/<int:id>", methods=["POST"])
def erase(table, id):
    """
    Remove a record and report database dependency conflicts safely.

    :param table: (URL) Table name.
    :param id: (URL) Primary key ID of the record to delete.
    :return: Redirects back to the table view with a success or error message.
    """
    if table not in classes:
        abort(404)

    data = db.session.get(classes[table], id)
    if data is None:
        flash("That record no longer exists.", "error")
        return redirect(url_for("tables_builder", param=table))

    record_label = {
        "user": "user",
        "lakes": "lake",
        "samples": "sample",
        "sampling_metadata": "sampling record",
        "taxa": "taxon",
        "model_versions": "model version",
        "model_runs": "model run",
        "predictions": "prediction",
        "reviews": "review",
    }.get(table, "record")

    try:
        db.session.delete(data)
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        app.logger.warning(
            "Deletion blocked by database references: table=%s id=%s",
            table,
            id,
            exc_info=True,
        )
        flash(
            f"Cannot delete this {record_label} because other records still "
            "reference it. Reassign or delete the dependent records first.",
            "error",
        )
    except SQLAlchemyError:
        db.session.rollback()
        app.logger.exception("Database error deleting table=%s id=%s", table, id)
        if app.debug:
            raise
        flash(
            f"The {record_label} could not be deleted because of a database "
            "error. No changes were saved.",
            "error",
        )
    else:
        flash(f"The {record_label} was deleted.", "success")

    return redirect(url_for("tables_builder", param=table))


def save_box_events(boxes, learner, selected_model):
    """Replace saved boxes and yield progress as each crop is classified."""
    committed = False
    total = len(boxes)
    try:
        db.session.execute(
            delete(Samples).where(Samples.sampling_metadata_id == tmp_image.meta_id)
        )
        model_run = start_model_run(
            tmp_image.meta_id,
            parameters={"box_count": total},
            model_version=selected_model if learner is not None else None,
        )
        tmp_image.contour_array = boxes

        for current, box in enumerate(boxes, start=1):
            yield {
                "status": "progress",
                "phase": "classifying",
                "current": current - 1,
                "item": current,
                "total": total,
            }
            values = tmp_image.coords_to_base64_with_classification(
                box[:4],
                learner is not None,
                learner=learner,
            )
            sample = Samples(**values)
            db.session.add(sample)
            record_prediction(
                sample,
                model_run=model_run,
                source="model" if learner is not None else "import",
            )
            yield {
                "status": "progress",
                "phase": "classified",
                "current": current,
                "item": current,
                "total": total,
            }

        model_run.status = "completed"
        model_run.completed_at = utc_now()
        db.session.commit()
        committed = True

        stmt = select(
            Samples.x,
            Samples.y,
            Samples.w,
            Samples.h,
            Samples.species,
            Samples.confidence,
        ).where(Samples.sampling_metadata_id == tmp_image.meta_id)
        rows = db.session.execute(stmt).all()
        tmp_image.contour_array = [list(row) for row in rows]
        yield {
            "status": "success",
            "current": total,
            "total": total,
            "boxes": json.dumps(tmp_image.contour_array),
        }
    finally:
        if not committed:
            db.session.rollback()


def stream_save_box_events(events):
    """Serialize classification progress without exposing internal errors."""
    try:
        for event in events:
            yield json.dumps(event) + "\n"
    except Exception:
        app.logger.exception("Classification failed while saving boxes")
        yield json.dumps(
            {
                "status": "error",
                "error": "Classification failed. No changes were saved.",
            }
        ) + "\n"


@app.route("/save_boxes", methods=["POST"])
def save_boxes():
    """Replace saved boxes, optionally streaming per-crop progress as NDJSON."""
    data = request.get_json()
    boxes = data.get("boxes", [])

    selected_model = selected_model_version()
    try:
        learner = load_model_version(selected_model) if selected_model else None
    except ModelArtifactError as error:
        return jsonify({"status": "error", "error": str(error)}), 409

    events = save_box_events(boxes, learner, selected_model)
    if "application/x-ndjson" in request.headers.get("Accept", ""):
        response = Response(
            stream_with_context(stream_save_box_events(events)),
            mimetype="application/x-ndjson",
        )
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Accel-Buffering"] = "no"
        return response

    final_event = None
    for final_event in events:
        pass
    return jsonify(final_event)


@app.route("/update_boxes", methods=["POST"])
def update_boxes():
    """
    Updates, deletes, or inserts specific bounding box classifications.
    Logic handles:
    - Deletion (confidence -1)
    - Manual Species Updates
    - Automated classification for unlabelled boxes.

    :param update_boxes: (JSON) Array of box data including coordinates, species, and IDs.
    :return: JSON success status.
    """
    data = request.get_json()
    update_boxes = data.get("update_boxes", [])
    visible_boxes = data.get("boxes")
    needs_inference = any(
        box is not None and box[6] == -1 and box[4] == "Unlabeled"
        for box in update_boxes
    )
    selected_model = selected_model_version() if needs_inference else None
    try:
        learner = load_model_version(selected_model) if selected_model else None
    except ModelArtifactError as error:
        return jsonify({"status": "error", "error": str(error)}), 409

    model_run = None
    try:
        if visible_boxes is not None:
            retained_ids = {
                int(box[6])
                for box in visible_boxes
                if box is not None and len(box) > 6 and box[6] != -1
            }
            delete_missing = delete(Samples).where(
                Samples.sampling_metadata_id == tmp_image.meta_id
            )
            if retained_ids:
                delete_missing = delete_missing.where(Samples.id.not_in(retained_ids))
            db.session.execute(delete_missing)

        for box in update_boxes:
            if box is None:
                continue
            elif box[5] == -1:  # Delete flag
                sample = db.session.get(Samples, box[6])
                if sample is not None:
                    db.session.delete(sample)
            elif box[6] != -1:  # Update existing row
                sample = db.session.get(Samples, box[6])
                if sample is not None:
                    record_review(sample, box[4])
            elif box[6] == -1 and box[4] == "Unlabeled":  # New row with classification
                if model_run is None:
                    model_run = start_model_run(
                        tmp_image.meta_id,
                        parameters={"operation": "update_boxes"},
                        model_version=selected_model if learner is not None else None,
                    )
                values = tmp_image.coords_to_base64_with_classification(
                    box[:4],
                    learner is not None,
                    learner=learner,
                )
                sample = Samples(**values)
                db.session.add(sample)
                record_prediction(
                    sample,
                    model_run=model_run,
                    source="model" if learner is not None else "import",
                )
            else:  # New row with manual label
                sample = Samples(
                    sampling_metadata_id=tmp_image.meta_id,
                    x=box[0],
                    y=box[1],
                    w=box[2],
                    h=box[3],
                    species="Unlabeled",
                    confidence=0.0,
                    image_base64=tmp_image.coords_to_base64_no_classification(box[:4]),
                )
                db.session.add(sample)
                record_review(sample, box[4])
        if model_run is not None:
            model_run.status = "completed"
            model_run.completed_at = utc_now()
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    return jsonify({"status": "success", "boxes": "updated"})


@app.route("/mask_slider", methods=["POST"])
def mask_slider():
    """
    Adjusts the image mask and bounding boxes based on threshold values from a slider.

    :param from: (JSON) Lower threshold.
    :param to: (JSON) Upper threshold.
    :return: JSON containing the updated base64 mask and contour array.
    """
    data = request.get_json()
    from_value = data.get("from")
    to_value = data.get("to")

    tmp_image.create_mask(thresh_1=from_value, thresh_2=to_value)
    tmp_image.draw_boxes(thresh_1=from_value, thresh_2=to_value)

    return jsonify(
        {
            "img_data_mask": tmp_image.mask_io.decode("utf-8"),
            "img_boxes": json.dumps(tmp_image.contour_array),
        }
    )


def next_unreviewed_species(image_id, current_species):
    """Return the next taxon with unreviewed samples for one source image."""
    species_names = db.session.execute(
        select(Samples.species)
        .where(
            Samples.sampling_metadata_id == image_id,
            Samples.review_status != "reviewed",
            Samples.species != current_species,
        )
        .group_by(Samples.species)
        .order_by(func.lower(Samples.species), Samples.species)
    ).scalars().all()

    if not species_names:
        return None

    current_key = current_species.casefold()
    return next(
        (name for name in species_names if name.casefold() > current_key),
        species_names[0],
    )


@app.route("/pages/classification/<int:image_id>/<species>", methods=["GET"])
def show_all_images(image_id, species):
    """
    Displays all sample images belonging to a specific species for a specific parent image.

    :param image_id: (URL) ID of the sampling metadata.
    :param species: (URL) The specific species name to filter by.
    :return: Rendered HTML gallery of specific species samples.
    """
    stmt = (
        select(Samples.id, Samples.image_base64, Samples.review_status)
        .where(
            and_(Samples.sampling_metadata_id == image_id, Samples.species == species)
        )
        .order_by(Samples.review_status, Samples.id)
    )
    rows = db.session.execute(stmt).mappings().all()
    unreviewed_count = sum(row.review_status != "reviewed" for row in rows)

    sample_species = db.session.execute(select(Samples.species).distinct()).scalars()
    taxa_species = db.session.execute(select(Taxon.scientific_name)).scalars()
    species_list = sorted(
        ({*sample_species, *taxa_species} - {species}), key=str.casefold
    )
    next_species = next_unreviewed_species(image_id, species)

    return render_template(
        "pages/all_images.html",
        rows=rows,
        species_list=species_list,
        image_id=image_id,
        species=species,
        unreviewed_count=unreviewed_count,
        next_species=next_species,
    )


@app.route("/review-species/<int:image_id>/<species>", methods=["POST"])
def approve_species_review(image_id, species):
    """Approve all unreviewed samples for one source image and species label."""
    samples = db.session.execute(
        select(Samples).where(
            Samples.sampling_metadata_id == image_id,
            Samples.species == species,
            Samples.review_status != "reviewed",
        )
    ).scalars().all()

    try:
        for sample in samples:
            record_review(
                sample,
                sample.species,
                notes="Bulk-approved from the species review gallery.",
            )
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        app.logger.exception(
            "Bulk review failed for image_id=%s species=%s",
            image_id,
            species,
        )
        flash("The bulk review could not be saved. No changes were applied.", "error")
    else:
        if samples:
            flash(
                f"Approved {len(samples)} {species} sample"
                f"{'s' if len(samples) != 1 else ''}.",
                "success",
            )
        else:
            flash("All samples in this group are already reviewed.", "success")

    if request.form.get("review_action") == "next":
        next_species = next_unreviewed_species(image_id, species)
        if next_species:
            return redirect(
                url_for(
                    "show_all_images", image_id=image_id, species=next_species
                )
            )
        flash("There are no other unreviewed taxa for this image.", "success")

    return redirect(url_for("show_all_images", image_id=image_id, species=species))


@app.route("/save-species", methods=["POST"])
def move_species():
    """
    Bulk updates or deletes species classifications for samples.

    :param data: (JSON) List of objects containing 'id' and 'species'.
    :return: JSON success status.
    """
    data = request.get_json(silent=True)
    if not isinstance(data, list) or not data:
        return jsonify({"status": "error", "message": "No label changes supplied."}), 400

    try:
        for item in data:
            if not isinstance(item, dict) or "id" not in item or "species" not in item:
                raise ValueError("Every label change requires an id and species.")
            sample = db.session.get(Samples, item["id"])
            species = str(item["species"]).strip()
            if sample and species:
                if species == "delete":
                    db.session.delete(sample)
                else:
                    record_review(
                        sample,
                        species,
                        notes="Corrected from the species review gallery.",
                    )
        db.session.commit()
    except (SQLAlchemyError, TypeError, ValueError):
        db.session.rollback()
        app.logger.exception("Saving species review corrections failed")
        return jsonify(
            {"status": "error", "message": "The label changes could not be saved."}
        ), 400

    next_url = None
    image_id = request.args.get("image_id", type=int)
    current_species = request.args.get("species", type=str)
    if request.args.get("next") == "1" and image_id and current_species:
        next_species = next_unreviewed_species(image_id, current_species)
        if next_species:
            next_url = url_for(
                "show_all_images", image_id=image_id, species=next_species
            )

    return jsonify({"status": "ok", "next_url": next_url})


@app.route("/pages/data_explorer", methods=["GET", "POST"])
def query_builder():
    """
    Loads the interactive data explorer UI with schema metadata.

    :return: Rendered HTML with tables, relationships, and join graphs.
    """
    return render_template(
        "pages/data_explorer.html",
        tables=get_tables(),
        relationships=get_relationships(),
        join_graph=build_join_graph(),
        table_columns=get_table_columns(),
    )


@app.route("/pages/run_query", methods=["POST"])
def run_query():
    """
    Executes a custom-built query generated by the data explorer.

    :param payload: (JSON) Query parameters and filters.
    :return: List of dictionaries representing the result rows.
    """
    payload = request.get_json()
    stmt = build_query(payload)
    results = db.session.execute(stmt).all()
    return [dict(row._mapping) for row in results]


@app.route("/pages/run_sql_query", methods=["POST"])
def run_sql_query():
    """Execute one guarded, read-only SQL statement for advanced exploration."""
    payload = request.get_json(silent=True) or {}

    try:
        rows = execute_read_only_query(payload.get("query", ""))
    except ReadOnlyQueryError as error:
        return jsonify({"error": str(error)}), 400
    except Exception as error:
        app.logger.info("Advanced SQL query rejected: %s", error)
        message = getattr(error, "orig", error)
        return jsonify({"error": f"Query could not be executed: {message}"}), 400

    return jsonify(
        {
            "rows": rows,
            "row_count": len(rows),
        }
    )


@app.route("/pages/training", methods=["GET"])
def training_page():
    """
    Renders the machine learning model training and upload management page.

    :return: Rendered HTML for training management.
    """
    return render_template(
        "/pages/training_page.html",
        model_versions=available_model_versions(),
        selected_model=selected_model_version(),
        training_species=get_training_species_counts(),
    )


@app.route("/download/samples-by-species", methods=["GET", "POST"])
def download_samples_by_species():
    """
    Generate a configured train/validation ZIP organized by species.

    :return: Binary ZIP attachment or a redirect after invalid options.
    """
    options = None
    if request.method == "POST":
        try:
            species_targets = json.loads(
                request.form.get("species_targets", "{}")
            )
        except json.JSONDecodeError:
            species_targets = None
        if not isinstance(species_targets, dict):
            flash("Custom species targets are invalid.", "error")
            return redirect(url_for("training_page"))
        options = {
            "review_scope": request.form.get("review_scope"),
            "species": request.form.getlist("species"),
            "distribution": request.form.get("distribution"),
            "target_per_species": request.form.get("target_per_species"),
            "species_targets": species_targets,
            "validation_percent": request.form.get("validation_percent"),
            "seed": request.form.get("seed"),
            "augmentations": request.form.getlist("augmentations"),
            "augmentation_copies": request.form.get("augmentation_copies"),
        }

    try:
        zip_buffer = build_species_zip(options)
    except TrainingExportError as error:
        flash(str(error), "error")
        return redirect(url_for("training_page"))

    return send_file(
        zip_buffer,
        mimetype="application/zip",
        as_attachment=True,
        download_name=f"plankton_training_set_{date.today().isoformat()}.zip",
    )


@app.route("/upload-model", methods=["POST"])
def upload_model():
    """
    Handles the upload of a pre-trained .pkl classifier model.

    :param model_file: (File) The .pkl file to be used as the new image learner.
    :return: Redirects to training page with success or error message.
    """
    file = request.files.get("model_file")

    if not file or not file.filename or not file.filename.lower().endswith(".pkl"):
        flash("Please upload a valid .pkl model file", "error")
        return redirect(url_for("training_page"))

    default_name = Path(secure_filename(file.filename)).stem
    model_name = request.form.get("model_name", "").strip() or default_name
    if not model_name or len(model_name) > 120:
        flash("Model names must contain between 1 and 120 characters.", "error")
        return redirect(url_for("training_page"))

    storage_directory = model_storage_directory()
    storage_directory.mkdir(parents=True, exist_ok=True)
    temporary_path = None

    try:
        with tempfile.NamedTemporaryFile(
            dir=storage_directory,
            prefix=".upload-",
            suffix=".pkl",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
        file.save(temporary_path)

        digest = hashlib.sha256(temporary_path.read_bytes()).hexdigest()
        learner = tmp_image.load_learner_from_file(
            temporary_path,
            raise_errors=True,
        )
        artifact_path = storage_directory / f"{digest}.pkl"
        if artifact_path.exists():
            temporary_path.unlink()
            temporary_path = None
        else:
            os.replace(temporary_path, artifact_path)
            temporary_path = None

        try:
            stored_path = str(artifact_path.relative_to(PROJECT_ROOT))
        except ValueError:
            stored_path = str(artifact_path)
        model_version = db.session.execute(
            select(ModelVersion).where(
                ModelVersion.name == model_name,
                ModelVersion.version == digest,
            )
        ).scalar_one_or_none()
        if model_version is None:
            model_version = ModelVersion(
                name=model_name,
                version=digest,
                artifact_path=stored_path,
            )
            db.session.add(model_version)
        else:
            model_version.artifact_path = stored_path

        ModelVersion.query.update(
            {ModelVersion.is_active: False},
            synchronize_session=False,
        )
        model_version.is_active = True
        db.session.flush()
        db.session.commit()

        session["selected_model_version_id"] = model_version.id
        _model_learner_cache.clear()
        cache_key = (model_version.id, str(artifact_path), artifact_path.stat().st_mtime_ns)
        _model_learner_cache[cache_key] = learner
    except Exception:
        db.session.rollback()
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()
        app.logger.warning("Model upload failed validation", exc_info=True)
        flash(
            "The model could not be loaded. No model was activated.",
            "error",
        )
        return redirect(url_for("training_page"))

    flash(f"{model_name} was uploaded and set as the default model.", "success")
    return redirect(url_for("training_page"))


@app.route("/models/select", methods=["POST"])
def select_model():
    """Select a model version for the current browser session."""
    model_version = db.session.get(
        ModelVersion,
        request.form.get("model_version_id", type=int),
    )
    if model_version is None:
        flash("Select an available model version.", "error")
        return redirect(model_redirect_target())

    try:
        load_model_version(model_version)
    except ModelArtifactError as error:
        flash(str(error), "error")
    else:
        session["selected_model_version_id"] = model_version.id
        flash(
            f"Using {model_version.name} ({model_version.version[:12]}) for this session.",
            "success",
        )
    return redirect(model_redirect_target())


@app.route("/models/<int:model_version_id>/default", methods=["POST"])
def set_default_model(model_version_id):
    """Validate and set a model version as the application-wide default."""
    model_version = db.session.get(ModelVersion, model_version_id)
    if model_version is None:
        abort(404)

    try:
        load_model_version(model_version)
    except ModelArtifactError as error:
        flash(str(error), "error")
        return redirect(model_redirect_target())

    ModelVersion.query.update(
        {ModelVersion.is_active: False},
        synchronize_session=False,
    )
    model_version.is_active = True
    db.session.commit()
    session["selected_model_version_id"] = model_version.id
    flash(
        f"{model_version.name} ({model_version.version[:12]}) is now the default model.",
        "success",
    )
    return redirect(model_redirect_target())
