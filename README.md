# Zooplankton Classification Suite

A Flask application for detecting objects in microscopy images, classifying image
crops with a fastai model, storing results in SQLite, and exporting labeled training
data.

## Run locally

Clone the repository and use the launcher for your operating system. No existing
Python installation is required.

### Linux

```bash
./planktonlab
```

### macOS

Double-click `planktonlab.command`, or run:

```bash
./planktonlab.command
```

### Windows

Double-click `planktonlab.cmd`, or run it from Command Prompt. The checked-in
PowerShell implementation is available as `planktonlab.ps1` for terminal users.

On first launch, the script downloads the pinned `uv` bootstrap, Python 3.12, and
the locked application dependencies. Later launches reuse that local environment and
can run offline. The browser opens automatically at <http://127.0.0.1:5000>.

The default dependency profile uses CPU-only PyTorch packages. NVIDIA CUDA users on
Linux or Windows can opt in before launching:

```bash
PLANKTONLAB_ACCELERATOR=cuda ./planktonlab
```

```powershell
$env:PLANKTONLAB_ACCELERATOR = "cuda"
.\planktonlab.cmd
```

Set `PLANKTONLAB_OPEN_BROWSER=false` to suppress automatic browser opening. Set
`PLANKTONLAB_HOST` or `PLANKTONLAB_PORT` to change the listener. The default listener
is restricted to the local machine. `DEBUG=True` enables Flask debug behavior, but
Waitress remains the HTTP server.

Application data is stored outside the Git checkout so pulls and branch changes do
not affect the database, uploaded images, models, or session key:

- Linux: `$XDG_DATA_HOME/PlanktonLab` or `~/.local/share/PlanktonLab`
- macOS: `~/Library/Application Support/PlanktonLab`
- Windows: `%LOCALAPPDATA%\PlanktonLab`

Set `PLANKTONLAB_DATA_DIR` to override this location.

The application uses the headless OpenCV package because image processing runs on the
server and does not open desktop windows. This avoids requiring system graphics
libraries such as `libGL.so.1`.

For development, install `uv` and synchronize the checked-in lock file:

```bash
uv --system-certs sync --frozen --extra cpu
PLANKTONLAB_OPEN_BROWSER=false uv run --no-sync python run.py
```

Dependency downloads and runtime lake lookups use the operating system certificate
store, so organization-managed certificate authorities are honored without disabling
TLS verification.

At startup, the application creates the current SQLite schema and reporting views when
they do not exist. Database migration from older application versions is intentionally
not performed; use a new data directory when moving from an incompatible schema.

Uploaded FastAI learners are validated and retained as content-addressed files in the
user data directory. The Training page can set a global default or choose a different
model for the current browser session; classification runs record the exact model
version used. Set `MODEL_STORAGE_FOLDER` to use another non-public directory.

The Training page also builds reproducible datasets with reviewed-status filtering,
species selection, natural/balanced/custom class counts, source-aware train/validation
splits, optional image augmentation, and a JSON manifest describing every exported file.

## Documentation

See the [complete user manual](docs/USER_MANUAL.md) for the illustrated end-to-end
workflow, every user-facing option, model and dataset management, security guidance,
and troubleshooting.

See [the API reference](docs/API_REFERENCE.md) for:

- configuration and application lifecycle;
- database models and query-building helpers;
- image detection and classification methods;
- every Flask route and its request/response contract;
- Python and HTTP usage examples; and
- relevant error, state, and security considerations.

See the [database ER diagram](docs/ER_DIAGRAM.md) for the current tables, foreign-key
cardinalities, delete behavior, and reporting views.

When the application is running, the same reference is available at
<http://127.0.0.1:5000/docs> and from the **Documentation** navigation item.
