# Zooplankton Classification Suite API Reference

This reference documents the Python modules, database models, utility functions,
image-processing methods, and Flask routes in the project. Examples assume commands
are run from the repository root through the locked environment:

```bash
uv run --no-sync python
```

## Application lifecycle

`apps/__init__.py` creates the shared Flask application, loads `Config`, and imports
`apps.views` to register routes. `run.py` creates the current SQLite schema and starts
Waitress on the local-only port 5000. When application `DEBUG`
configuration is disabled, `run.py` also enables HTML minification.

The preferred entry points download the pinned `uv` bootstrap and Python 3.12 when
needed, synchronize the platform-specific lock, and then invoke `run.py`:

```bash
./planktonlab             # Linux
./planktonlab.command     # macOS
planktonlab.cmd           # Windows
```

Use `PLANKTONLAB_ACCELERATOR=cpu|cuda` to select the PyTorch build,
`PLANKTONLAB_VENV_DIR` to select the virtual-environment directory, and
`PLANKTONLAB_HOST` or `PLANKTONLAB_PORT` to change the listener. Manual startup remains
available after `uv sync --frozen --extra cpu`:

```bash
uv run --no-sync python run.py
```

Waitress listens on `127.0.0.1` by default. Set `PLANKTONLAB_OPEN_BROWSER=false` to
disable automatic browser opening.

`apps.views.tmp_image` is a process-global `Image_Classifier` instance. Image state is
therefore shared by requests handled by the same process; the current design is intended
for a single-user development workflow rather than concurrent production traffic.

## Configuration

### `apps.config.Config`

Provides configuration values consumed by the Flask application.

| Attribute | Type | Default | Description |
| --- | --- | --- | --- |
| `DEBUG` | `bool` | `False` | Enabled only when the `DEBUG` environment variable is exactly `"True"`. |
| `DATA_DIRECTORY` | `str` | OS user-data directory | Parent directory for mutable local application data. |
| `UPLOAD_FOLDER` | `str` | `<data>/uploads` | Filesystem directory used for uploaded source images. |
| `MODEL_STORAGE_FOLDER` | `str` | `<data>/models` | Non-public directory used for immutable, content-addressed model artifacts. |
| `SQLITE_DATABASE_PATH` | `str` | `<data>/application.db` | Absolute path to the required SQLite database. |
| `SQLALCHEMY_DATABASE_URI` | `str` | derived from the database path | SQLite connection URI. Other database engines are intentionally unsupported. |
| `SECRET_KEY` | `str` | persistent generated key | Flask signing key stored as `<data>/session.key`, unless overridden. |

Example:

```bash
DEBUG=True \
PLANKTONLAB_DATA_DIR=/tmp/planktonlab/ \
UPLOAD_FOLDER=/tmp/zooplankton-uploads/ \
MODEL_STORAGE_FOLDER=/tmp/zooplankton-models/ \
SQLITE_DATABASE_PATH=/tmp/zooplankton.db \
SECRET_KEY='replace-this-value' \
uv run --no-sync python run.py
```

## Database models

The models and helpers live in
`apps/static/assets/utils/database_classes.py`. All examples that access `db` must run
inside a Flask application context.

### `Base`

Custom SQLAlchemy declarative base. Its `metadata` defines deterministic names for
indexes and constraints.

### `TimestampMixin`

Adds UTC `created_at` and `updated_at` fields. It is used by every application model.

### `User`

Represents an application operator.

| Field | Type | Notes |
| --- | --- | --- |
| `id` | `int` | Auto-incrementing primary key. |
| `username` | `str` | Unique username. |
| `email` | `str` | User email address. |
| `created_at`, `updated_at` | `datetime` | UTC audit timestamps. |

### `Lakes`

Stores sampling-location metadata.

| Field | Type | Notes |
| --- | --- | --- |
| `id` | `int` | Auto-incrementing primary key. |
| `lake` | `str` | Required lake name; unique together with `country`. |
| `country` | `str` | Required country name. |
| `depth` | `Optional[float]` | Optional maximum depth in metres. |
| `area` | `Optional[float]` | Optional surface area in square kilometres. |
| `latitude`, `longitude` | `Optional[float]` | Optional WGS84 coordinates. |
| `elevation_m`, `mean_depth_m` | `Optional[float]` | Optional elevation and mean depth in metres. |
| `volume_km3` | `Optional[float]` | Optional volume in cubic kilometres. |
| `shoreline_length_km` | `Optional[float]` | Optional shoreline length in kilometres. |
| `catchment_area_km2` | `Optional[float]` | Optional catchment area in square kilometres. |
| `residence_time_days` | `Optional[float]` | Optional water residence time in days. |
| `waterbody_type`, `administrative_region` | `Optional[str]` | Optional location descriptors. |
| `external_source`, `external_id`, `source_url` | `Optional[str]` | Optional metadata provenance. |
| `metadata_retrieved_at` | `Optional[datetime]` | UTC timestamp for the imported metadata. |
| `created_at`, `updated_at` | `datetime` | UTC audit timestamps. |

### `Sampling_metadata`

Represents one source image and its sampling event.

| Field | Type | Notes |
| --- | --- | --- |
| `id` | `int` | Auto-incrementing primary key. |
| `user_id` | `int` | Required foreign key to `User`; deletion is restricted. |
| `lake_id` | `int` | Required foreign key to `Lakes`; deletion is restricted. |
| `image_name` | `str` | Unique, required source filename. |
| `sampling_date` | `date` | Required collection date. |
| `image_path` | `str` | Required path to the stored source image. |
| `user` | `User` | SQLAlchemy relationship to the operator. |
| `lake` | `Lakes` | SQLAlchemy relationship to the lake. |
| `created_at`, `updated_at` | `datetime` | UTC audit timestamps. |

### `Taxon`

Normalizes classification labels while allowing a parent/child taxonomy.

| Field | Type | Notes |
| --- | --- | --- |
| `id` | `int` | Primary key. |
| `scientific_name` | `str` | Unique normalized label. |
| `common_name` | `str` or `None` | Optional display name. |
| `rank` | `str` | Taxonomic rank; `Unlabeled` uses `unclassified`. |
| `parent_id` | `int` or `None` | Optional self-reference to the parent taxon. |

### `ModelVersion`

Records classifier provenance independently from model executions.

| Field | Type | Notes |
| --- | --- | --- |
| `name`, `version` | `str` | Unique model/version pair. Uploaded models use a SHA-256 prefix as the version. |
| `artifact_path` | `str` or `None` | Local model artifact path. |
| `metrics_json` | `str` or `None` | Optional serialized evaluation metrics. |
| `is_active` | `bool` | Whether new runs should use this version. |

### `ModelRun`

Tracks one classifier execution for one sampling image.

| Field | Type | Notes |
| --- | --- | --- |
| `model_version_id` | `int` or `None` | Model used for the run. |
| `sampling_metadata_id` | `int` | Source image; cascades on deletion. |
| `status` | `str` | `running`, `completed`, or `failed`. |
| `parameters_json` | `str` or `None` | Serialized run settings. |
| `started_at`, `completed_at` | `datetime` | Execution timing. |
| `error_message` | `str` or `None` | Failure information when available. |

### `Samples`

Stores one detected image crop and its classification.

| Field | Type | Notes |
| --- | --- | --- |
| `id` | `int` | Auto-incrementing primary key. |
| `sampling_metadata_id` | `int` | Required parent-image foreign key; cascades on deletion. |
| `x`, `y` | `int` | Top-left crop coordinate in pixels. |
| `w`, `h` | `int` | Crop width and height in pixels. |
| `species` | `str` | Compatibility snapshot of the current predicted or reviewed class. |
| `confidence` | `float` | Current confidence constrained to `0` through `1`; manual labels use `1.0`. |
| `image_base64` | `str` | Required base64-encoded crop image. |
| `taxon_id` | `int` or `None` | Normalized current taxon. |
| `model_run_id` | `int` or `None` | Model run that produced the current prediction. |
| `review_status` | `str` | `unreviewed` or `reviewed`. |
| `sampling_metadata` | `Sampling_metadata` | SQLAlchemy relationship to the source image. |

Bounding-box origins must be non-negative, dimensions must be positive, and indexes
cover image, species, review status, and common compound lookups.

### `Prediction`

Stores immutable prediction history separately from the current `Samples` snapshot.
Each row references a sample and normalized taxon, with optional model-run provenance,
a confidence from `0` through `1`, and source `model` or `import`.

### `Review`

Stores immutable human review history. A review references the sample, optional
reviewer, chosen taxon, previous label, optional notes, and a status of `accepted`,
`corrected`, or `rejected`.

Model creation example:

```python
from apps import app
from apps.static.assets.utils.database_classes import Lakes, User, db

with app.app_context():
    user = User(username="operator-1", email="operator@example.org")
    lake = Lakes(lake="Lake Example", country="Switzerland")
    db.session.add_all([user, lake])
    db.session.commit()
```

## Database utility functions

### `initialize_database()`

Creates the current SQLite schema and recreates its reporting views.

- Parameters: None; requires an application context.
- Returns: `None`.
- Adds: Tables, constraints, indexes, foreign-key enforcement, and reporting views for
  a new database.
- Idempotency: Re-running the function against the current schema is safe.
- Compatibility: Older schemas are not upgraded automatically; select a new data
  directory or migrate the database explicitly before launching this version.
- Raises: `RuntimeError` if the configured database is not SQLite.

```python
from apps import app
from apps.static.assets.utils.database_classes import initialize_database

with app.app_context():
    initialize_database()
```

The reporting views are:

- `classification_summary`: counts and average confidence by image and taxon;
- `review_queue`: unreviewed detections, including their base64 crops; and
- `model_run_summary`: model provenance and prediction counts per run.

### `get_or_create_taxon(species, rank="species")`

Returns the existing exact-name taxon or creates and flushes one. Empty labels become
`Unlabeled` with rank `unclassified`.

### `start_model_run(sampling_metadata_id, parameters=None, model_version=None)`

Creates and flushes a `running` model run linked to the explicitly selected model
version. Optional parameters are serialized as JSON. Passing `None` records a run
without a model, such as an import-only classification.

### `record_prediction(sample, model_run=None, source="model")`

Normalizes a sample's current label, marks it unreviewed, links its model run, and
adds an immutable `Prediction`. Returns the new prediction object.

### `record_review(sample, species, reviewer_id=None, notes=None)`

Marks the sample reviewed and adds an immutable `Review`. Accepting the existing label
preserves its model confidence and model-run provenance. Correcting the label sets
confidence to `1.0` and clears the current model-run link. Returns the review object.

### `get_tables()`

Returns `list[str]` containing every table registered in SQLAlchemy metadata.

```python
from apps.static.assets.utils.database_classes import get_tables

table_names = get_tables()
# Includes application tables such as taxa, model_versions, predictions, and reviews.
```

### `get_columns(table_name)`

Returns column metadata for one table.

- `table_name`: Exact key in `db.Model.metadata.tables`.
- Returns: `list[dict]`; every dictionary contains `name`, SQL type text in `type`,
  and the Boolean `primary_key` flag.
- Raises: `KeyError` if the table is unknown.

```python
columns = get_columns("samples")
# [{"name": "id", "type": "INTEGER", "primary_key": True}, ...]
```

### `get_relationships()`

Inspects all foreign keys and returns `list[dict[str, str]]`. Each dictionary contains
`from_table`, `from_column`, `to_table`, and `to_column`.

```python
relationships = get_relationships()
# One entry links samples.sampling_metadata_id to sampling_metadata.id.
```

### `build_join_graph()`

Builds a directed adjacency list from `get_relationships()`.

- Parameters: None.
- Returns: `dict[str, list[str]]` mapping a foreign-key-owning table to referenced tables.

```python
graph = build_join_graph()
assert "sampling_metadata" in graph["samples"]
```

### `get_table_columns()`

Returns the selectable schema used by the data-explorer UI.

- Parameters: None.
- Returns: A dictionary keyed by model table and available reporting-view names; each
  value is a list of column dictionaries with `name` and `type` keys. View columns
  also include `view: true`.

```python
schema = get_table_columns()
sample_names = [column["name"] for column in schema["samples"]]
```

### `build_select(payload)`

Creates the initial SQLAlchemy `Select` statement for a data-explorer request.

- `payload`: Dictionary containing `base_table` and `columns`. `base_table` is a key
  in `classes`. `columns` maps table names to selected column names; an empty list
  selects all columns from that table.
- Returns: A SQLAlchemy `Select` whose `FROM` clause starts with the base model.
- Raises: `KeyError` for unknown tables and `AttributeError` for unknown columns.

```python
payload = {
    "base_table": "samples",
    "columns": {"samples": ["species", "confidence"]},
    "joins": [],
    "conditions": [],
}
statement = build_select(payload)
```

### `apply_joins(stmt, payload)`

Adds explicit equality joins to an existing select statement.

- `stmt`: SQLAlchemy statement returned by `build_select`.
- `payload`: Dictionary with a `joins` list. Each join requires `from_table`,
  `from_column`, `to_table`, and `to_column`.
- Returns: A new SQLAlchemy statement containing all requested joins.
- Raises: `KeyError` for unknown tables and `AttributeError` for unknown columns.

```python
payload["joins"] = [{
    "from_table": "samples",
    "from_column": "sampling_metadata_id",
    "to_table": "sampling_metadata",
    "to_column": "id",
}]
statement = apply_joins(statement, payload)
```

### `build_where(payload)`

Converts data-explorer conditions into a SQLAlchemy Boolean expression. Supported
operators are `=`, `!=`, `>`, `<`, `>=`, `<=`, and `LIKE`. Conditions may include
`openParen`, `closeParen`, and `logic` fields, although the current implementation
combines completed expressions with SQL `AND`.

- `payload`: Dictionary containing a `conditions` list. Each condition must include
  `table`, `column`, `operator`, and `value`.
- Returns: A SQLAlchemy Boolean expression, or `None` when no conditions are supplied.
- Raises: `KeyError` for unknown tables/operators and `AttributeError` for unknown columns.

```python
payload["conditions"] = [{
    "table": "samples",
    "column": "confidence",
    "operator": ">=",
    "value": 0.8,
    "logic": "AND",
    "openParen": 0,
    "closeParen": 0,
}]
where_clause = build_where(payload)
```

### `build_query(payload)`

Assembles column selection, joins, and conditions into one executable query.

- `payload`: Complete data-explorer payload accepted by `build_select`, `apply_joins`,
  and `build_where`.
- Returns: A SQLAlchemy `Select` statement. It does not execute the statement.

```python
from apps import app
from apps.static.assets.utils.database_classes import build_query, db

payload = {
    "base_table": "samples",
    "columns": {"samples": ["species", "confidence"]},
    "joins": [],
    "conditions": [],
}
with app.app_context():
    rows = db.session.execute(build_query(payload)).all()
```

### `validate_read_only_query(sql)`

Validates SQL submitted through the advanced data-explorer editor. It accepts one
`SELECT` statement or a `WITH` query whose body is read-only. Comments and quoted
values are ignored during structural checks.

- `sql`: SQL string to validate.
- Returns: The trimmed SQL string.
- Raises: `ReadOnlyQueryError` for empty input, multiple statements, unterminated
  quoted values, non-query statements, or write/schema-management keywords.

```python
query = validate_read_only_query("SELECT species, COUNT(*) FROM samples GROUP BY species")
```

### `execute_read_only_query(sql)`

Executes advanced SQL with read-only safeguards. SQLite uses `PRAGMA query_only`, a
three-second progress timeout, and returns all matching rows.

- `sql`: A query accepted by `validate_read_only_query`.
- Returns: A list containing every matching row as a dictionary.
- Raises: `ReadOnlyQueryError` for unsafe SQL; SQLAlchemy database errors for invalid
  tables, columns, or syntax.

```python
with app.app_context():
    rows = execute_read_only_query("SELECT id, species FROM samples ORDER BY id")
```

### `get_training_species_counts()`

Returns eligible sample counts used by the Training-page export dialog.

- Parameters: None.
- Returns: A species-sorted list of dictionaries containing `species`, `reviewed`, and
  `total` counts. The `Unlabeled` compatibility class is excluded.
- Side effects: Executes a query through the current Flask-SQLAlchemy session.

```python
with app.app_context():
    counts = get_training_species_counts()
```

### `normalize_training_export_options(options=None)`

Validates the export contract and applies defaults. It returns normalized review scope,
species, distribution, targets, validation percentage, seed, augmentations, and copy
count. Invalid or unavailable selections raise `TrainingExportError`.

### `build_species_zip(options=None)`

Creates an in-memory, reproducible ZIP containing `train/<species>/`,
`valid/<species>/`, and `manifest.json`.

- `review_scope`: `reviewed` or `all`.
- `species`: List of exact species labels.
- `distribution`: `natural`, `balanced`, or `custom`.
- `target_per_species`: Base count for balanced mode.
- `species_targets`: Name-to-count mapping for custom mode.
- `validation_percent`: Integer from 1 through 40.
- `seed`: Integer from 0 through 4294967295.
- `augmentations`: Any of `rotate`, `horizontal_flip`, `vertical_flip`,
  `brightness_contrast`, `zoom`, `noise`, and `blur`.
- `augmentation_copies`: Extra augmented copies per training original, from 0 through
  10, for natural-distribution exports. Balanced/custom exports keep exact final targets
  and use augmentation only to fill shortages.
- Returns: A rewound `io.BytesIO` archive.
- Behavior: Splits by parent sampling image where possible, never augments validation
  images, converts all exported images to PNG, and uses augmentation to fill balanced
  or custom shortages. The manifest records source IDs, transformations, settings,
  warnings, and final counts.
- Raises: `TrainingExportError` for invalid options, invalid base64, or undecodable images.

```python
with app.app_context():
    archive = build_species_zip({
        "review_scope": "reviewed",
        "species": ["Calanoida", "Cyclopoida"],
        "distribution": "balanced",
        "target_per_species": 100,
        "validation_percent": 20,
        "seed": 42,
        "augmentations": ["rotate", "brightness_contrast"],
        "augmentation_copies": 1,
    })
    with open("training-set.zip", "wb") as output:
        output.write(archive.read())
```

## Image processing and classification

The `Image_Classifier` class lives in
`apps/static/assets/utils/image_class.py`. Despite its historical underscore name, it
acts as a stateful image-processing session.

### `Image_Classifier.__init__()`

Creates an empty classifier without loading a model. The object stores the current
grayscale image, mask, encoded images, detected boxes, parent metadata ID, and learner.
The learner remains `None` until a selected versioned model is loaded explicitly;
without one, crops receive the label `"Unlabeled"` with confidence `0.0`.

```python
from apps.static.assets.utils.image_class import Image_Classifier

classifier = Image_Classifier()
```

### `load_learner_from_file(model_path, raise_errors=False)`

Loads a trusted, versioned fastai learner on the CPU.

- `model_path`: Required path to a versioned `.pkl` artifact.
- `raise_errors`: Re-raise deserialization errors when validating uploads.
- Returns: A fastai `Learner`, or `None` after printing an error when loading fails and
  `raise_errors` is false.
- Security: Fastai learner files use Python pickle. Load only trusted model files.

```python
classifier.learner = classifier.load_learner_from_file("path/to/trusted-model.pkl")
```

### `read_image(path)`

Reads an image with OpenCV and converts it from BGR color to grayscale.

- `path`: String or path-like filesystem location of an OpenCV-readable image.
- Returns: `None`.
- Side effects: Stores the grayscale array in `orig_img`.
- Raises: `cv2.error` when the path cannot be decoded and color conversion receives
  an empty image.

```python
classifier.read_image("apps/static/assets/media/sample.jpg")
print(classifier.orig_img.shape)
```

### `io_image()`

JPEG-encodes `orig_img` and `mask`, then base64-encodes both for HTML data URLs.

- Parameters: None.
- Returns: `None`.
- Side effects: Sets `curr_image_io` and `mask_io` to base64 `bytes`.
- Preconditions: `orig_img` and `mask` must contain PIL-compatible NumPy arrays.

```python
classifier.create_mask()
raw_data_url = "data:image/jpeg;base64," + classifier.curr_image_io.decode("utf-8")
```

### `create_mask(thresh_1=75, thresh_2=200)`

Applies OpenCV binary thresholding to `orig_img` and refreshes the encoded source and
mask images.

- `thresh_1`: Numeric cutoff. Pixels above it receive `thresh_2`; other pixels become 0.
- `thresh_2`: Numeric maximum value written to pixels above the cutoff.
- Returns: `None`.
- Side effects: Replaces `mask`, `mask_io`, and `curr_image_io`.
- Preconditions: Call `read_image` first.

```python
classifier.read_image("sample.jpg")
classifier.create_mask(thresh_1=80, thresh_2=255)
```

### `draw_boxes(use_otsu=True, thresh_1=75, thresh_2=200, padding_ratio=0.15)`

Detects likely organisms and stores padded bounding boxes. Processing consists of a
Gaussian blur, Otsu or manual thresholding, morphological close/open operations,
external-contour extraction, dynamic area filtering, aspect-ratio filtering, and an
extent check.

- `use_otsu`: When `True`, calculate the threshold automatically. When `False`, use
  `thresh_1` and `thresh_2`.
- `thresh_1`: Manual threshold cutoff; ignored with Otsu enabled.
- `thresh_2`: Manual maximum pixel value; ignored with Otsu enabled.
- `padding_ratio`: Fraction of each accepted box's width and height added as padding.
  Padding is clipped to image bounds. Use `0` for unpadded boxes.
- Returns: `None`.
- Side effects: Replaces `mask`, encoded image buffers, and `contour_array`. Each box is
  stored as `[x, y, width, height]`. If no candidate exceeds the 150-pixel baseline,
  `contour_array` remains empty.

```python
classifier.read_image("sample.jpg")
classifier.draw_boxes(use_otsu=True, padding_ratio=0.10)
for x, y, width, height in classifier.contour_array:
    print(x, y, width, height)
```

### `numpy_to_base64(img_np, format="JPEG")`

Encodes a NumPy image through Pillow and returns base64 text.

- `img_np`: PIL-compatible NumPy array.
- `format`: Pillow output format such as `"JPEG"` or `"PNG"`.
- Returns: UTF-8 `str` containing base64 data without a data-URL prefix.
- Raises: Pillow exceptions for unsupported formats or incompatible arrays.

```python
encoded = classifier.numpy_to_base64(classifier.orig_img, format="PNG")
html_source = f"data:image/png;base64,{encoded}"
```

### `coords_to_base64_no_classification(cont)`

Crops `orig_img` without model inference and encodes the result as JPEG base64.

- `cont`: Four-item sequence `[x, y, width, height]` in pixels.
- Returns: Base64 `str` for the cropped image.
- Preconditions: `orig_img` must be loaded and the crop must have nonzero dimensions.

```python
crop = classifier.coords_to_base64_no_classification([10, 20, 30, 40])
```

### `coords_to_base64_with_classification(cont, has_learner=True)`

Crops one box, optionally invokes the fastai learner, and constructs a sample row.

- `cont`: Four-item sequence `[x, y, width, height]` in pixels.
- `has_learner`: If `True`, call `self.learner.predict`; if `False`, emit an unlabeled
  result. Pass `False` whenever `self.learner` is `None`.
- Returns: A dictionary containing the parent ID, coordinates, predicted species,
  rounded maximum confidence, and base64 crop.
- Raises: `AttributeError` if `has_learner=True` but no learner is loaded; prediction
  errors from the loaded model are also propagated.

```python
result = classifier.coords_to_base64_with_classification(
    [10, 20, 30, 40],
    has_learner=classifier.learner is not None,
)
```

## Flask HTTP routes

Route functions read form/query/JSON input from Flask's request context. The Python
function signatures therefore list only URL path parameters; request-body parameters
are documented below.

Examples use this base URL for a locally running application:

```bash
BASE_URL=http://127.0.0.1:5000
```

### `documentation()` — `GET /docs`

Reads this checked-in Markdown file, converts it to HTML with raw HTML disabled, and
renders it within the application layout for quick reference.

- Parameters: None.
- Returns: Rendered HTML documentation page.
- Errors: HTTP 404 if `docs/API_REFERENCE.md` is unavailable.

```bash
curl "$BASE_URL/docs"
```

### `index()` — `GET /`

Renders the landing page when the workspace is ready. If no operator or no lake exists,
it redirects to the first-run setup route.

- Parameters: None.
- Returns: HTML response from `pages/landing_page.html`, or an HTTP redirect to
  `/setup` when required metadata is missing.

```bash
curl -L "$BASE_URL/"
```

### `onboarding_requirements()`

Checks whether the minimum metadata needed by the classification workflow exists.

- Parameters: None; requires an application context.
- Returns: `dict[str, bool]` with `user` and `lake` keys. A `True` value means that
  record must be created before classification can begin.

```python
from apps import app
from apps.views import onboarding_requirements

with app.app_context():
    missing = onboarding_requirements()
```

### `setup()` — `GET|POST /setup`

Provides first-run onboarding. GET renders fields for only the missing operator or
lake. POST validates and creates those records in one transaction, then sends the user
directly to image-source selection. Already configured workspaces are redirected to
image-source selection without showing the form.

- Form `username`: Required initial operator name when no user exists.
- Form `email`: Required initial operator email when no user exists.
- Form `lake`: Required unique lake name when no lake exists.
- Form `country`: Required country when no lake exists.
- Form `depth`: Optional non-negative maximum depth in metres.
- Form `area`: Optional non-negative surface area in square kilometres.
- Additional lake fields: Optional coordinates, physical measurements, location
  descriptors, and source provenance. Name and country are sufficient for offline use.
- Returns: Setup HTML on GET or after validation failure; redirect to
  `/pages/select_image_source` after success or when setup is already complete.
- Side effects: Creates the missing `User` and/or `Lakes` row in the local SQLite
  database. A failed submission rolls back the whole transaction.

```bash
curl -i -X POST "$BASE_URL/setup" \
  -d username='Jane Smith' \
  -d email='jane.smith@example.org' \
  -d lake='Walchensee' \
  -d country='Germany'
```

### `lake_metadata_search()` — `GET /api/lakes/search`

Searches Wikidata for lake candidates and normalizes available metadata into the
units used by `Lakes`. The user must confirm a candidate before form values are
populated; lookup failures never prevent manual entry.

- Query `name`: Required lake name with at least two characters.
- Query `country`: Optional full country name used to filter ambiguous results.
- Returns: JSON containing `results` and `manual_entry: true`.
- Errors: HTTP 400 for an invalid name and HTTP 503 when Wikidata is unavailable.
- External access: Uses the fixed Wikidata API endpoint with an identifying user agent
  and an eight-second timeout. HTTPS validation uses the operating system trust store,
  including organization-managed certificates.

```bash
curl "$BASE_URL/api/lakes/search?name=Walchensee&country=Germany"
```

### `select_source()` — `GET|POST /pages/select_image_source`

On GET, renders the source-selection form with all users, lakes, and stored source
images. On POST, either redirects to an existing image or saves and processes a new
upload before creating its `Sampling_metadata` row.

- Form `action`: `load_from_db` or `classify_new_image`.
- Form `db_selection`: Existing sampling metadata ID, required for `load_from_db`.
- File `image_file`: Uploaded image, required for `classify_new_image`.
- Form `user_selection`: Operator ID for a new image.
- Form `lake_selection`: Lake ID for a new image.
- Form `date_selection`: Sampling date in `YYYY-MM-DD` format.
- Returns: HTML on GET; redirect to the classification route on success; redirect back
  for an empty filename; plain text with HTTP 400 if the upload part is absent. GET and
  POST redirect to `/setup` when either the initial operator or lake is missing.

```bash
curl -X POST "$BASE_URL/pages/select_image_source" \
  -F action=classify_new_image \
  -F image_file=@sample.jpg \
  -F user_selection=1 \
  -F lake_selection=1 \
  -F date_selection=2026-09-06
```

### `load_from_db(image_id)` — `GET /pages/classification/<image_id>`

Loads the source image, regenerates its mask, retrieves saved boxes, and renders the
classification editor. The toolbar's **Review Results** action links directly to the
alphabetically first taxon with unreviewed samples, falling back to the first available
taxon when every sample is reviewed. It is disabled when the image has no saved samples.

- `image_id`: Integer primary key of a `Sampling_metadata` row.
- Returns: HTML response containing base64 source/mask images and serialized boxes.
- Setup guard: Redirects to `/setup` when either the initial operator or lake is
  missing.
- Side effects: Replaces the process-global classifier's current image, metadata ID,
  mask, and contour array.

```bash
curl "$BASE_URL/pages/classification/1"
```

### `pages_database()` — `GET /pages/database`

Renders the database overview with all SQLAlchemy table names.

- Parameters: None.
- Returns: HTML response from `pages/database.html`.

```bash
curl "$BASE_URL/pages/database"
```

### `tables_builder(param)` — `GET /table/<param>`

Renders one database table with ten rows per page.

- `param`: Model key such as `user`, `lakes`, `sampling_metadata`, or `samples`.
- Query `page`: Optional one-based page number; defaults to 1.
- Returns: Paginated HTML response.
- Errors: HTTP 404 when `param` is not registered in `classes`.

```bash
curl "$BASE_URL/table/samples?page=2"
```

### `add_data(param)` — `GET /add_data/<param>`

Renders a typed, model-derived form for adding a database row. Primary keys and the
server-managed `created_at` and `updated_at` columns are never included in the form.
Columns with model or server defaults may be left empty.

- `param`: Table name used to look up SQLAlchemy columns.
- Returns: HTML response from `pages/add_db_entry.html`.
- Errors: HTTP 404 for an unknown table name.

```bash
curl "$BASE_URL/add_data/lakes"
```

### `insert_do_db()` — `GET|POST /add`

Creates a model instance from allow-listed form fields. A special `table` field selects
the model. Values are converted according to their SQLAlchemy Boolean, date, datetime,
integer, or floating-point type before construction.

- Form `table`: Required model key in `classes`.
- Remaining form fields: Editable model columns; unknown and server-managed fields are
  ignored.
- Returns: Redirect to `/table/<table>` after a successful POST, the populated form
  after validation/database failure, or `/pages/database` after GET.
- Side effects: Commits the new row. `created_at` and `updated_at` are generated by the
  model automatically. Failures roll back the transaction and display a safe message.

```bash
curl -X POST "$BASE_URL/add" \
  -d table=user \
  -d username=operator-1 \
  -d email=operator@example.org
```

### `edit_database_row(table, id)` — `GET|POST /edit/<table>/<id>`

Edits one existing row through the same typed form. The primary key and audit
timestamps are excluded. GET pre-populates current values; POST applies only
allow-listed editable columns.

- `table`: Model key in `classes`.
- `id`: Integer primary key of the row.
- Returns: Populated edit form on GET or after a failure; redirect to
  `/table/<table>` after success.
- Side effects: Preserves `created_at`, refreshes `updated_at`, and commits the changed
  values. Validation, uniqueness, and foreign-key failures roll back the transaction.
- Errors: HTTP 404 for an unknown table or missing row.

```bash
curl -X POST "$BASE_URL/edit/user/3" \
  -d username=operator-renamed \
  -d email=renamed@example.org
```

### `erase(table, id)` — `POST /delete/<table>/<id>`

Deletes one row and redirects to its table page. Foreign-key conflicts are rolled back
and shown as a safe, plain-language flash message; technical details are written only
to the application log. Other SQLAlchemy errors are re-raised in debug mode and shown
as a generic database error when debug mode is disabled.

- `table`: Model key in `classes`.
- `id`: Integer primary key.
- Returns: Redirect to `/table/<table>` with a success or error message; HTTP 404 for
  an unknown table.
- Side effects: Deletes and commits the selected row, or rolls back the transaction on
  failure.

```bash
curl -L -X POST "$BASE_URL/delete/user/3"
```

### `save_boxes()` — `POST /save_boxes`

Replaces every saved crop for the classifier's current source image. Each supplied box
is classified when a learner is available.

- JSON `boxes`: List of `[x, y, width, height]` arrays.
- Returns: JSON `{"status": "success", "boxes": "<JSON string>"}`. The nested `boxes`
  value contains coordinates, species, and confidence.
- When requested with `Accept: application/x-ndjson`, returns newline-delimited
  `progress` events before the final `success` event. Progress events include
  `phase`, `current`, `item`, and `total` fields for a single persistent UI bar.
- Side effects: Atomically replaces old `Samples` rows, preserves every crop in
  `image_base64`, creates a `ModelRun`, appends normalized `Prediction` rows, and
  replaces `tmp_image.contour_array`.
- Preconditions: Load a classification page first so `tmp_image.meta_id` and
  `tmp_image.orig_img` are set.

```bash
curl -X POST "$BASE_URL/save_boxes" \
  -H 'Content-Type: application/json' \
  -d '{"boxes":[[10,20,30,40],[70,80,25,35]]}'
```

### `update_boxes()` — `POST /update_boxes`

Applies incremental box changes. Each item has the shape
`[x, y, width, height, species, confidence, id]`.

- JSON `update_boxes`: List containing box arrays or `null` entries.
- JSON `boxes`: Optional complete list of boxes still visible on the canvas. When
  provided, persisted samples missing from this list are deleted, including when the
  list is empty after **Remove All Boxes**.
- `confidence == -1`: Delete the sample identified by `id`.
- Existing `id != -1`: Apply a normalized label and append a `Review`.
- New `id == -1`, species `Unlabeled`: Classify, encode, and append a `Prediction`.
- Other new rows: Store the manually labeled base64 crop and append a `Review`.
- Returns: JSON `{"status": "success", "boxes": "updated"}`.
- Side effects: Applies the complete batch in one transaction.

```bash
curl -X POST "$BASE_URL/update_boxes" \
  -H 'Content-Type: application/json' \
  -d '{"update_boxes":[[10,20,30,40,"Daphnia",0.5,12]]}'
```

### `mask_slider()` — `POST /mask_slider`

Regenerates the mask and detected boxes for the current image.

- JSON `from`: Lower manual threshold. If omitted, `create_mask` receives `None`.
- JSON `to`: Maximum manual threshold. If omitted, `create_mask` receives `None`.
- Returns: JSON with base64 `img_data_mask` and JSON-serialized `img_boxes`.
- Side effects: Updates the global classifier's mask and contour array. `draw_boxes`
  currently uses Otsu by default, so its own manual threshold values are ignored.

```bash
curl -X POST "$BASE_URL/mask_slider" \
  -H 'Content-Type: application/json' \
  -d '{"from":75,"to":255}'
```

### `show_all_images(image_id, species)` — `GET /pages/classification/<image_id>/<species>`

Renders all stored crops for one source image and species, plus alternative species
labels for reassignment, review-state indicators, and a bulk approval action.

- `image_id`: Parent sampling metadata ID from the URL.
- `species`: Species name from the URL.
- Returns: HTML response from `pages/all_images.html`.

```bash
curl "$BASE_URL/pages/classification/1/Daphnia"
```

### `approve_species_review(image_id, species)` — `POST /review-species/<image_id>/<species>`

Approves every currently unreviewed sample in the specified image/species group.

- Scope: Samples must match both URL values and have `review_status != "reviewed"`.
- Side effects: Appends an `accepted` review for each sample and marks it reviewed while
  retaining the original label, confidence, and model-run provenance.
- Returns: Redirect to the same species gallery with a success or error message.
- Repeating the request is safe: already reviewed samples are skipped.

```bash
curl -X POST "$BASE_URL/review-species/1/Daphnia"
```

### `move_species()` — `POST /save-species`

Bulk relabels or deletes sample crops.

- JSON body: A list of objects with integer `id` and string `species`.
- Species `delete`: Deletes the row.
- A different species updates the label, sets confidence to `1.0`, and appends a
  corrected `Review`. Selecting the existing species records an accepted review while
  preserving model confidence and provenance.
- Returns: JSON `{"status": "ok"}`.
- Side effects: Commits all changes once after processing the list.

```bash
curl -X POST "$BASE_URL/save-species" \
  -H 'Content-Type: application/json' \
  -d '[{"id":12,"species":"Daphnia"},{"id":13,"species":"delete"}]'
```

### `query_builder()` — `GET|POST /pages/data_explorer`

Renders the interactive query builder with table names, relationships, the join graph,
and column metadata.

- Parameters: None. POST bodies are not consumed.
- Returns: HTML response from `pages/data_explorer.html`.

```bash
curl "$BASE_URL/pages/data_explorer"
```

### `run_query()` — `POST /pages/run_query`

Builds and executes a data-explorer query.

- JSON body: Complete payload described by `build_query`.
- Returns: A JSON array containing all matching result-row objects. Flask serializes
  the returned Python list.
- Side effects: Executes a read query against the database.

```bash
curl -X POST "$BASE_URL/pages/run_query" \
  -H 'Content-Type: application/json' \
  -d '{
    "base_table":"samples",
    "columns":{"samples":["species","confidence"]},
    "joins":[],
    "conditions":[{
      "table":"samples",
      "column":"confidence",
      "operator":">=",
      "value":0.8,
      "logic":"AND",
      "openParen":0,
      "closeParen":0
    }]
  }'
```

### `run_sql_query()` — `POST /pages/run_sql_query`

Runs one guarded read-only statement from the advanced SQL editor.

- JSON body: An object with a `query` string containing one `SELECT` or read-only
  `WITH` statement.
- Returns: An object with `rows` and `row_count`. No application row limit is applied.
- Errors: HTTP 400 with an `error` message for unsafe statements or invalid SQL.
- Safety: Write and schema-management keywords, PRAGMAs, and multiple statements are
  rejected. SQLite also runs in query-only mode with a short timeout.

```bash
curl -X POST "$BASE_URL/pages/run_sql_query" \
  -H 'Content-Type: application/json' \
  -d '{"query":"SELECT species, COUNT(*) AS count FROM samples GROUP BY species"}'
```

### `training_page()` — `GET /pages/training`

Renders the model-training and model-upload page.

- Parameters: None.
- Returns: HTML response from `pages/training_page.html`.

```bash
curl "$BASE_URL/pages/training"
```

### `download_samples_by_species()` — `GET|POST /download/samples-by-species`

Builds and downloads a configured training ZIP. POST is used by the Training-page
dialog; GET produces the default reviewed-only, natural-distribution export.

- Repeated form `species`: Exact labels to include.
- Form `review_scope`: `reviewed` or `all`.
- Form `distribution`: `natural`, `balanced`, or `custom`.
- Form `target_per_species`: Balanced base target.
- Form `species_targets`: JSON object for custom targets.
- Form `validation_percent`, `seed`, and `augmentation_copies`: Integer settings.
- Repeated form `augmentations`: Selected transformation names.
- Returns: `application/zip` named `plankton_training_set_<date>.zip`, or redirects to
  Training with an error message when validation fails.

```bash
curl -OJ -X POST "$BASE_URL/download/samples-by-species" \
  -d review_scope=reviewed \
  -d distribution=balanced \
  -d target_per_species=100 \
  -d validation_percent=20 \
  -d seed=42 \
  -d species=Calanoida \
  -d species=Cyclopoida \
  -d augmentations=rotate \
  -d augmentation_copies=1
```

### `upload_model()` — `POST /upload-model`

Validates a trusted fastai learner, stores it under its full SHA-256 digest, registers
it as a `ModelVersion`, and makes it the default and current-session model. Existing
artifacts are never overwritten.

- File `model_file`: Required multipart file whose name ends in `.pkl`.
- Form `model_name`: Optional display name. The filename stem is used when omitted.
- Returns: Redirect to the training page with a success or error flash message.
- Side effects: Creates `MODEL_STORAGE_FOLDER`, stores `<sha256>.pkl`, registers or
  activates the matching `ModelVersion`, and stores its ID in the signed browser session.
- Failure behavior: Invalid or unloadable files are removed and the previously active
  model remains unchanged.
- Security: A learner is pickle-backed and may execute code during loading. Upload only
  files produced by a trusted source.

```bash
curl -X POST "$BASE_URL/upload-model" \
  -F model_name=lake-classifier \
  -F model_file=@classifier.pkl
```

### `select_model()` — `POST /models/select`

Validates and selects a registered model for the current browser session. It does not
change the application-wide default.

- Form `model_version_id`: Required integer `ModelVersion` ID.
- Form `next`: Optional local redirect path. External redirect targets are rejected.
- Returns: Redirect with a success or error flash message.

```bash
curl -c session.txt -b session.txt -X POST "$BASE_URL/models/select" \
  -d model_version_id=2 \
  -d next=/pages/training
```

### `set_default_model(model_version_id)` — `POST /models/<model_version_id>/default`

Validates a registered artifact and makes it the default for sessions that have not
selected another model. The current browser session also switches to it.

- `model_version_id`: Integer URL identifier.
- Form `next`: Optional safe local redirect path.
- Returns: Redirect, `404` for an unknown version, or a flash error when loading fails.

```bash
curl -X POST "$BASE_URL/models/2/default" -d next=/pages/training
```

During classification, the selected version is loaded from a small in-process cache and
passed explicitly to inference. Each `ModelRun.model_version_id` therefore records the
actual runtime selection rather than merely whichever version is globally marked active.

## End-to-end Python example

The following example runs detection and prepares rows without requiring a trained
classifier:

```python
from apps.static.assets.utils.image_class import Image_Classifier

classifier = Image_Classifier()
classifier.read_image("sample.jpg")
classifier.draw_boxes(use_otsu=True)
classifier.meta_id = 1

rows = [
    classifier.coords_to_base64_with_classification(box, has_learner=False)
    for box in classifier.contour_array
]
for row in rows:
    print(row["species"], row["x"], row["y"], row["confidence"])
```

## Error and trust boundaries

- Table and column identifiers in data-explorer payloads are dynamically resolved.
  Validate or authorize callers before exposing these endpoints beyond trusted users.
- Uploaded filenames and model files are accepted by application routes. Treat uploads
  as untrusted input and retain the existing trusted-user boundary.
- Uploaded model artifacts are loaded through Python pickle semantics and must never
  come from an untrusted source.
- Image methods assume a successfully decoded image and valid, in-bounds crop geometry.
  Invalid input is generally surfaced as OpenCV, Pillow, SQLAlchemy, or fastai errors.
