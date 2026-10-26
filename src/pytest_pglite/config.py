"""Configuration models for pytest-pglite.

All settings can be provided through environment variables using the
``PGLITE_`` prefix, for example ``PGLITE_ISOLATION=transaction``, or through
the pytest command line options documented in :mod:`pytest_pglite.plugin`.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_EXTENSION_RE = re.compile(r"^[a-z][a-z0-9_]*$")


class IsolationMode(StrEnum):
    """How per-test isolation is achieved."""

    NONE = "none"
    """No automatic isolation; the database keeps its state between tests."""

    SCHEMA = "schema"
    """Drop and recreate the ``public`` schema around each test (default)."""

    TRANSACTION = "transaction"
    """Roll back the fixture connection at the end of each test."""

    DATABASE = "database"
    """Reserved; not supported by the single-user PGlite backend."""


class PGliteConfig(BaseSettings):
    """Validated runtime configuration for a PGlite server."""

    model_config = SettingsConfigDict(
        env_prefix="PGLITE_",
        extra="ignore",
        env_file=None,
        enable_decoding=False,
    )

    pg_version: str = Field(
        default="17.5",
        description="PostgreSQL major.minor version of the bundled WASM build.",
    )
    extensions: tuple[str, ...] = Field(
        default=(),
        description="Extensions created in the database at startup.",
    )
    isolation: IsolationMode = Field(
        default=IsolationMode.SCHEMA,
        description="Per-test isolation strategy.",
    )
    work_dir: Path | None = Field(
        default=None,
        description="Directory for database files. Defaults to a temp directory.",
    )
    wasm_path: Path | None = Field(
        default=None,
        description="Directory containing a custom bin/pglite.wasi build.",
    )
    unix_socket: bool = Field(
        default=True,
        description="Listen on a Unix domain socket.",
    )
    tcp: bool = Field(
        default=False,
        description="Also listen on TCP.",
    )
    tcp_host: str = Field(default="127.0.0.1")
    tcp_port: int = Field(
        default=0,
        ge=0,
        le=65535,
        description="TCP port; 0 lets the operating system assign a free port.",
    )
    max_connections: int = Field(default=32, ge=1, le=1024)
    queue_timeout: float = Field(
        default=60.0,
        gt=0,
        description="Seconds a connection waits for the shared backend.",
    )
    rewrite_concurrent_index: bool = Field(
        default=True,
        description=(
            "Rewrite CREATE/DROP INDEX CONCURRENTLY and REINDEX CONCURRENTLY "
            "to their plain forms. The single-backend build cannot run a "
            "concurrent index build (upstream PGlite issue 901) and there are "
            "no concurrent writers to worry about."
        ),
    )
    startup_timeout: float = Field(default=120.0, gt=0)
    holder_timeout: float | None = Field(
        default=None,
        gt=0,
        description=(
            "Seconds a connection may hold the backend while idle. "
            "None waits forever (clients normally Sync promptly)."
        ),
    )
    aot_cache: bool = Field(
        default=True,
        description="Cache the compiled WebAssembly module on disk.",
    )
    template_cache: bool = Field(
        default=True,
        description=("Cache an initialised database directory so workers skip initdb."),
    )
    prepare: Callable[[Any], Any] | None = Field(
        default=None,
        description=(
            "Callable run once on a fresh server to build the application's "
            "schema, for example an Alembic upgrade. It receives the "
            "PGliteServer, whose dsn/url/execute are usable while it runs. "
            "The resulting data directory is cached and copied by every "
            "worker instead of each worker migrating."
        ),
    )
    prepare_key: str | None = Field(
        default=None,
        description=(
            "Caller-supplied schema revision mixed into the prepare cache key, "
            "for example 'alembic:0056'. Change it whenever the schema "
            "changes, or workers will reuse the old database."
        ),
    )
    prepare_cache: bool = Field(
        default=True,
        description=(
            "Cache the data directory produced by prepare. Independent of "
            "template_cache, which only covers the initdb half."
        ),
    )
    template_cache_max_mb: int = Field(
        default=2048,
        ge=0,
        description=(
            "Size cap for the template cache, in MiB. Least-recently-published "
            "templates are removed when it is exceeded; 0 disables pruning."
        ),
    )
    template_lock_timeout: float = Field(
        default=300.0,
        gt=0,
        description=(
            "Seconds a worker waits for another worker to build the same "
            "template. A dead builder releases its lock immediately; this "
            "only bounds a builder that hangs."
        ),
    )
    rebuild_templates: bool = Field(
        default=False,
        description=(
            "Ignore and replace cached templates. Also set with "
            "PGLITE_REBUILD_TEMPLATES=1 or --pglite-rebuild-templates."
        ),
    )
    keep_tmp: bool = Field(default=False)
    log_level: str = Field(default="INFO")
    user: str = Field(default="postgres")
    database: str = Field(default="template1")
    password: str = Field(default="password")

    # ------------------------------------------------------------------
    # guest heap and instance lifecycle
    # ------------------------------------------------------------------
    restart_every_n_tests: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Restart the PGlite instance after this many tests, keeping the "
            "data directory. WebAssembly linear memory can grow but never "
            "shrink, so a long suite ratchets the guest heap up until an "
            "allocation finally fails; a periodic restart returns it to its "
            "starting size and costs a fraction of a second."
        ),
    )
    heap_restart_mb: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Restart the instance when the guest heap exceeds this many MiB, "
            "checked between tests. Use it alone or together with "
            "restart_every_n_tests."
        ),
    )
    restart_on_out_of_memory: bool = Field(
        default=True,
        description=(
            "Restart the instance when the backend reports out of memory "
            "(SQLSTATE 53200), so one exhausted test does not fail every "
            "subsequent test in the run."
        ),
    )
    oom_restart_limit: int = Field(
        default=5,
        ge=0,
        description=(
            "Give up restarting after this many out-of-memory restarts in one "
            "session, so a workload that genuinely cannot fit fails fast."
        ),
    )
    session_settings: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Extra SET name=value pairs applied to the backend session at "
            "startup, merged over the WASM-tuned defaults (a 16 MB "
            "maintenance_work_mem, no parallel workers, jit off). Set a value "
            "to '' to drop one of the defaults."
        ),
    )

    # ------------------------------------------------------------------
    # host memory configuration (wasmtime)
    # ------------------------------------------------------------------
    memory_reservation_mb: int | None = Field(
        default=None,
        ge=0,
        description=(
            "Initial virtual address space reserved per guest memory, in MiB. "
            "The default reserves several GiB per instance, which is wasteful "
            "when many instances run at once (for example pytest-xdist "
            "workers). Lowering it does not lower the guest ceiling."
        ),
    )
    memory_reservation_for_growth_mb: int | None = Field(
        default=None,
        ge=0,
        description="Extra address space reserved per memory after a growth move.",
    )
    memory_guard_size_mb: int | None = Field(
        default=None,
        ge=0,
        description="Guard region reserved after each guest memory, in MiB.",
    )
    memory_may_move: bool | None = Field(
        default=None,
        description=(
            "Whether a guest memory may be relocated to grow. False makes "
            "memory_reservation a hard ceiling; True (the default) lets the "
            "memory move and keep growing."
        ),
    )
    max_wasm_stack_mb: int | None = Field(
        default=None,
        ge=1,
        description="Maximum WebAssembly operand stack per instance, in MiB.",
    )

    @field_validator("extensions", mode="before")
    @classmethod
    def _split_extensions(cls, value: object) -> object:
        if isinstance(value, str):
            return tuple(part.strip() for part in value.split(",") if part.strip())
        return value

    @field_validator("extensions")
    @classmethod
    def _check_extensions(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        seen: list[str] = []
        for name in value:
            normalised = name.strip().lower()
            if not normalised:
                continue
            if not _EXTENSION_RE.match(normalised):
                raise ValueError(
                    f"invalid extension name {name!r}; expected [a-z][a-z0-9_]*"
                )
            if normalised not in seen:
                seen.append(normalised)
        return tuple(seen)

    @field_validator("work_dir", "wasm_path", mode="before")
    @classmethod
    def _expand(cls, value: object) -> object:
        if isinstance(value, str) and value:
            return Path(value).expanduser()
        return value

    @field_validator("session_settings", mode="before")
    @classmethod
    def _decode_session_settings(cls, value: object) -> object:
        """Accept a JSON object from the environment.

        ``model_config`` turns off pydantic-settings' own JSON decoding (it
        would try to decode every complex field), so a mapping supplied as
        ``PGLITE_SESSION_SETTINGS='{"work_mem": "8MB"}'`` is decoded here.
        """
        if isinstance(value, str):
            try:
                decoded = json.loads(value)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"session_settings must be a JSON object: {exc}"
                ) from exc
            if not isinstance(decoded, dict):
                raise ValueError("session_settings must be a JSON object")
            return decoded
        return value

    @field_validator("log_level")
    @classmethod
    def _check_log_level(cls, value: str) -> str:
        level = value.upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
            raise ValueError("log_level must be one of DEBUG, INFO, WARNING, ERROR")
        return level
