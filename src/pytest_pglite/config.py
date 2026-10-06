"""Configuration models for pytest-pglite.

All settings can be provided through environment variables using the
``PGLITE_`` prefix, for example ``PGLITE_ISOLATION=transaction``, or through
the pytest command line options documented in :mod:`pytest_pglite.plugin`.
"""

from __future__ import annotations

import re
from enum import StrEnum
from pathlib import Path

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
    keep_tmp: bool = Field(default=False)
    log_level: str = Field(default="INFO")
    user: str = Field(default="postgres")
    database: str = Field(default="template1")
    password: str = Field(default="password")

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

    @field_validator("log_level")
    @classmethod
    def _check_log_level(cls, value: str) -> str:
        level = value.upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
            raise ValueError("log_level must be one of DEBUG, INFO, WARNING, ERROR")
        return level
