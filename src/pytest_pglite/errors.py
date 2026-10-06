"""Exception types raised by pytest-pglite."""

from __future__ import annotations


class PGliteError(Exception):
    """Base class for all pytest-pglite errors."""


class PGliteArtifactError(PGliteError):
    """The bundled WASM artifact is missing, corrupt, or unusable."""


class PGliteTrapError(PGliteError):
    """The WASM backend trapped while executing a request."""

    def __init__(self, message: str, backtrace: str = "") -> None:
        super().__init__(message)
        self.backtrace = backtrace


class PGliteUnrecoverableError(PGliteError):
    """The backend could not be recovered after a trap."""


class PGliteQueueTimeoutError(PGliteError):
    """A connection waited too long for the shared backend."""


class PGliteProtocolError(PGliteError):
    """A client sent a malformed PostgreSQL protocol message."""


class PGliteConfigurationError(PGliteError):
    """The requested configuration cannot be satisfied."""
