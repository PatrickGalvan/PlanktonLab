# -*- encoding: utf-8 -*-
"""Configuration defaults for the Flask application.

Environment variables can override runtime settings such as ``DEBUG``,
``UPLOAD_FOLDER``, and ``SECRET_KEY``. See
``docs/API_REFERENCE.md`` for the complete configuration reference and examples.
"""

import os
import secrets
from pathlib import Path

from platformdirs import user_data_path


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _absolute_path(value):
    """Resolve configured paths consistently on Windows, macOS, and Linux."""
    path = Path(value).expanduser()
    return path if path.is_absolute() else (PROJECT_ROOT / path).resolve()


def _runtime_paths():
    """Create the user-owned directories used for mutable application data."""
    data_directory = _absolute_path(
        os.getenv("PLANKTONLAB_DATA_DIR", user_data_path("PlanktonLab", appauthor=False))
    )
    data_directory.mkdir(parents=True, exist_ok=True)

    database_path = _absolute_path(
        os.getenv("SQLITE_DATABASE_PATH", data_directory / "application.db")
    )
    upload_directory = _absolute_path(
        os.getenv("UPLOAD_FOLDER", data_directory / "uploads")
    )
    model_directory = _absolute_path(
        os.getenv("MODEL_STORAGE_FOLDER", data_directory / "models")
    )
    database_path.parent.mkdir(parents=True, exist_ok=True)
    upload_directory.mkdir(parents=True, exist_ok=True)
    model_directory.mkdir(parents=True, exist_ok=True)

    return data_directory, database_path, upload_directory, model_directory


def _secret_key(data_directory):
    """Load a persistent local session key, creating it on first launch."""
    configured = os.getenv("SECRET_KEY")
    if configured:
        return configured

    key_path = data_directory / "session.key"
    if key_path.is_file():
        return key_path.read_text(encoding="utf-8").strip()

    key = secrets.token_urlsafe(48)
    key_path.write_text(key, encoding="utf-8")
    try:
        key_path.chmod(0o600)
    except OSError:
        pass
    return key


DEFAULT_DATA_DIRECTORY, DATABASE_PATH, UPLOAD_DIRECTORY, MODEL_DIRECTORY = _runtime_paths()


class Config:
    """Namespace of settings loaded by :class:`flask.Flask` at startup.

    This class is not instantiated directly; ``app.config.from_object(Config)``
    copies its uppercase attributes into the Flask configuration mapping.
    """
    DEBUG = os.getenv("DEBUG", "False") == "True"

    DATA_DIRECTORY = str(DEFAULT_DATA_DIRECTORY)
    UPLOAD_FOLDER = str(UPLOAD_DIRECTORY)
    MODEL_STORAGE_FOLDER = str(MODEL_DIRECTORY)
    SQLITE_DATABASE_PATH = str(DATABASE_PATH)
    SQLALCHEMY_DATABASE_URI = f"sqlite:///{DATABASE_PATH.as_posix()}"

    # App Config - the minimal footprint
    SECRET_KEY = _secret_key(DEFAULT_DATA_DIRECTORY)
