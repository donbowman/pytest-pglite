"""Native PGlite (PostgreSQL in WebAssembly) for Python test suites."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

from .config import IsolationMode, PGliteConfig
from .errors import (
    PGliteArtifactError,
    PGliteConfigurationError,
    PGliteError,
    PGliteProtocolError,
    PGliteQueueTimeoutError,
    PGliteTrapError,
    PGliteUnrecoverableError,
)
from .server import PGliteServer

try:
    __version__ = version("pytest-pglite")
except PackageNotFoundError:  # pragma: no cover - running from a source tree
    __version__ = "0.0.0.dev0"

__all__ = [
    "IsolationMode",
    "PGliteArtifactError",
    "PGliteConfig",
    "PGliteConfigurationError",
    "PGliteError",
    "PGliteProtocolError",
    "PGliteQueueTimeoutError",
    "PGliteServer",
    "PGliteTrapError",
    "PGliteUnrecoverableError",
    "__version__",
]
