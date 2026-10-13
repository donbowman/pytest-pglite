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


class PGliteMemoryError(PGliteError):
    """The WebAssembly guest ran out of memory.

    Raised when the backend reports SQLSTATE 53200.  This is not the host
    running out of RAM: PGlite runs PostgreSQL inside a 32-bit WebAssembly
    linear memory, which can only grow, never shrink, so a long enough session
    eventually reaches the ceiling and every subsequent allocation fails.  The
    recovery is to restart the instance over the same data directory, and the
    prevention is to restart it periodically (see ``restart_every_n_tests`` and
    ``heap_restart_mb`` in :class:`pytest_pglite.PGliteConfig`).

    The attributes are the evidence for a report: the heap in use when the
    failure was seen, the ceiling it hit (None when the module declares no
    maximum), and the restart count so far.
    """

    def __init__(
        self,
        message: str,
        *,
        heap_bytes: int | None = None,
        heap_limit_bytes: int | None = None,
        restarts: int = 0,
    ) -> None:
        super().__init__(message)
        self.heap_bytes = heap_bytes
        self.heap_limit_bytes = heap_limit_bytes
        self.restarts = restarts
