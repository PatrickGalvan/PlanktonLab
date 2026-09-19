"""Initialize PlanktonLab and serve it on the local machine with Waitress."""

import os
import socket
import threading
import time
import webbrowser

from flask_minify import Minify
from waitress import serve

from apps import app
from apps.static.assets.utils.database_classes import initialize_database


def environment_flag(name, default=False):
    """Return a case-insensitive boolean environment setting."""
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def port_is_available(host, port):
    """Return whether the configured TCP listener can be acquired."""
    probe_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    try:
        addresses = socket.getaddrinfo(probe_host, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return False

    for family, socket_type, protocol, _, address in addresses:
        try:
            with socket.socket(family, socket_type, protocol) as probe:
                probe.bind(address)
            return True
        except OSError:
            continue
    return False


def open_browser_when_ready(host, port):
    """Open the local UI after the Waitress listener starts accepting traffic."""
    browser_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((browser_host, port), timeout=0.5):
                webbrowser.open(f"http://{browser_host}:{port}")
                return
        except OSError:
            time.sleep(0.2)


def main():
    """Initialize local state, validate the listener, and start Waitress."""
    host = os.getenv("PLANKTONLAB_HOST", "127.0.0.1")
    try:
        port = int(os.getenv("PLANKTONLAB_PORT", "5000"))
    except ValueError as error:
        raise SystemExit("PLANKTONLAB_PORT must be an integer between 1 and 65535.") from error
    if not 1 <= port <= 65535:
        raise SystemExit("PLANKTONLAB_PORT must be an integer between 1 and 65535.")
    if not port_is_available(host, port):
        raise SystemExit(
            f"Cannot start PlanktonLab: {host}:{port} is unavailable. "
            "Close the other application or set PLANKTONLAB_PORT to another port."
        )

    if not app.config["DEBUG"]:
        Minify(app=app, html=True, js=False, cssless=False)

    with app.app_context():
        initialize_database()

    browser_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    local_url = f"http://{browser_host}:{port}"
    print(f"[PlanktonLab] Data directory: {app.config['DATA_DIRECTORY']}", flush=True)
    print(f"[PlanktonLab] Open {local_url} in your browser.", flush=True)

    if environment_flag("PLANKTONLAB_OPEN_BROWSER", default=True):
        threading.Thread(
            target=open_browser_when_ready,
            args=(host, port),
            daemon=True,
        ).start()

    serve(app, host=host, port=port, threads=4)


if __name__ == "__main__":
    main()
