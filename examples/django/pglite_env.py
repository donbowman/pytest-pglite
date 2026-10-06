"""Start a PGlite server for the Django example.

pytest-django imports the settings module while plugins are being configured,
which is before any pytest fixture can run. Starting the server from the
settings module keeps the fixture model out of the picture; the server is
created once per process and stopped at interpreter exit.
"""

from __future__ import annotations

import atexit
import os

from pytest_pglite import PGliteConfig, PGliteServer

_server: PGliteServer | None = None


def ensure_server() -> PGliteServer:
    global _server
    if _server is None:
        _server = PGliteServer(PGliteConfig())
        _server.start()
        socket_dir = _server.socket_dir
        assert socket_dir is not None
        os.environ.setdefault("PGLITE_HOST", str(socket_dir))
        os.environ.setdefault("PGLITE_PORT", "5432")
        atexit.register(_server.stop)
    return _server
