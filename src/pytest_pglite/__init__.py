"""Native PGlite (PostgreSQL in WebAssembly) for Python test suites."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

from .config import IsolationMode, PGliteConfig
from .engine import DEFAULT_SESSION_SETTINGS
from .errors import (
    PGliteArtifactError,
    PGliteConfigurationError,
    PGliteError,
    PGliteMemoryError,
    PGliteProtocolError,
    PGliteQueueTimeoutError,
    PGliteTrapError,
    PGliteUnrecoverableError,
)
from .server import PGliteServer, live_servers

try:
    __version__ = version("pytest-pglite")
except PackageNotFoundError:  # pragma: no cover - running from a source tree
    __version__ = "0.0.0.dev0"

__all__ = [
    "DEFAULT_SESSION_SETTINGS",
    "IsolationMode",
    "PGliteArtifactError",
    "PGliteConfig",
    "PGliteConfigurationError",
    "PGliteError",
    "PGliteMemoryError",
    "PGliteProtocolError",
    "PGliteQueueTimeoutError",
    "PGliteServer",
    "PGliteTrapError",
    "PGliteUnrecoverableError",
    "__version__",
    "live_servers",
]
