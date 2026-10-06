"""The pytest plugin: fixtures and command-line options."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from typing import Any

import pytest

from .config import IsolationMode, PGliteConfig
from .server import PGliteServer

_RESET_STATEMENTS = (
    "DROP SCHEMA IF EXISTS public CASCADE",
    "CREATE SCHEMA public",
)

_log = logging.getLogger("pytest_pglite")


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("pglite", "PGlite (PostgreSQL in WebAssembly)")
    group.addoption(
        "--pglite-extensions",
        action="store",
        default=None,
        metavar="LIST",
        help="Comma separated extensions to CREATE at startup (for example vector).",
    )
    group.addoption(
        "--pglite-isolation",
        action="store",
        default=None,
        choices=[mode.value for mode in IsolationMode],
        help="Per-test isolation: none, schema (default) or transaction.",
    )
    group.addoption(
        "--pglite-tcp",
        action="store_true",
        default=None,
        help="Also listen on TCP (127.0.0.1, OS-assigned port).",
    )
    group.addoption(
        "--pglite-tcp-address",
        action="store",
        default=None,
        metavar="HOST:PORT",
        help="Explicit TCP bind address; port 0 gets an OS-assigned free port.",
    )
    group.addoption(
        "--pglite-work-dir",
        action="store",
        default=None,
        metavar="PATH",
        help="Directory for database files (default: a fresh temporary directory).",
    )
    group.addoption(
        "--pglite-wasm",
        action="store",
        default=None,
        metavar="PATH",
        help="Use a custom bin/pglite.wasi build.",
    )
    group.addoption(
        "--pglite-keep-tmp",
        action="store_true",
        default=None,
        help="Keep the temporary database directory after the session.",
    )
    group.addoption(
        "--pglite-log-level",
        action="store",
        default=None,
        metavar="LEVEL",
        help="Engine log level: DEBUG, INFO, WARNING or ERROR.",
    )


def _config_from_options(config: pytest.Config) -> PGliteConfig:
    values: dict[str, Any] = {}
    extensions = config.getoption("pglite_extensions")
    if extensions is not None:
        values["extensions"] = tuple(
            part.strip() for part in extensions.split(",") if part.strip()
        )
    isolation = config.getoption("pglite_isolation")
    if isolation is not None:
        values["isolation"] = isolation
    tcp = config.getoption("pglite_tcp")
    if tcp is not None:
        values["tcp"] = bool(tcp)
    tcp_address = config.getoption("pglite_tcp_address")
    if tcp_address is not None:
        host, _, port = tcp_address.rpartition(":")
        if not host or not port.isdigit():
            raise pytest.UsageError(
                f"--pglite-tcp-address expects HOST:PORT, got {tcp_address!r}"
            )
        values["tcp"] = True
        values["tcp_host"] = host
        values["tcp_port"] = int(port)
    work_dir = config.getoption("pglite_work_dir")
    if work_dir is not None:
        values["work_dir"] = work_dir
    wasm = config.getoption("pglite_wasm")
    if wasm is not None:
        values["wasm_path"] = wasm
    keep_tmp = config.getoption("pglite_keep_tmp")
    if keep_tmp is not None:
        values["keep_tmp"] = bool(keep_tmp)
    log_level = config.getoption("pglite_log_level")
    if log_level is not None:
        values["log_level"] = log_level
    try:
        cfg = PGliteConfig(**values)
    except ValueError as exc:
        raise pytest.UsageError(f"invalid pglite configuration: {exc}") from exc
    if cfg.isolation is IsolationMode.DATABASE:
        raise pytest.UsageError(
            "isolation=database is not supported by the single-user PGlite backend; "
            "use schema or transaction isolation"
        )
    return cfg


def pytest_configure(config: pytest.Config) -> None:
    cfg = _config_from_options(config)
    config._pglite_config = cfg  # type: ignore[attr-defined]


def pytest_report_header(config: pytest.Config) -> str | None:
    cfg: PGliteConfig | None = getattr(config, "_pglite_config", None)
    if cfg is None:
        return None
    extensions = ",".join(cfg.extensions) if cfg.extensions else "none"
    return (
        f"pglite: PostgreSQL {cfg.pg_version} (native wasm), "
        f"isolation={cfg.isolation.value}, extensions={extensions}"
    )


# ----------------------------------------------------------------------
# session fixtures
# ----------------------------------------------------------------------
@pytest.fixture(scope="session")
def pglite_server(request: pytest.FixtureRequest) -> Iterator[PGliteServer]:
    """A running PGlite server. One instance per xdist worker."""
    cfg: PGliteConfig = getattr(request.config, "_pglite_config", PGliteConfig())
    server = PGliteServer(cfg)
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture(scope="session")
def pglite_dsn(pglite_server: PGliteServer) -> str:
    """A libpq connection string for the running server."""
    return pglite_server.dsn


@pytest.fixture(scope="session")
def pglite_url(pglite_server: PGliteServer) -> str:
    """A SQLAlchemy URL for the running server."""
    return pglite_server.url


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------
def _reset_schema_sync(connection: Any) -> None:
    with connection.cursor() as cursor:
        for statement in _RESET_STATEMENTS:
            cursor.execute(statement)
    connection.commit()


async def _reset_schema_async(connection: Any) -> None:
    async with connection.cursor() as cursor:
        for statement in _RESET_STATEMENTS:
            await cursor.execute(statement)
    await connection.commit()


def _reset_engine(engine: Any, mode: IsolationMode) -> None:
    if mode is IsolationMode.NONE:
        return
    with engine.begin() as connection:
        for statement in _RESET_STATEMENTS:
            connection.exec_driver_sql(statement)


def _connect(dsn: str) -> Any:
    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover - extra not installed
        raise pytest.UsageError(
            "pglite_conn requires psycopg; install pytest-pglite[async] "
            "or add psycopg to your test dependencies"
        ) from exc
    return psycopg.connect(dsn)


# ----------------------------------------------------------------------
# function fixtures
# ----------------------------------------------------------------------
@pytest.fixture
def pglite_conn(pglite_dsn: str, pglite_server: PGliteServer) -> Iterator[Any]:
    """A psycopg connection with a freshly isolated schema."""
    mode = pglite_server.config.isolation
    connection = _connect(pglite_dsn)
    if mode is IsolationMode.SCHEMA:
        _reset_schema_sync(connection)
    elif mode is IsolationMode.TRANSACTION:
        connection.rollback()
    try:
        yield connection
    finally:
        try:
            if mode is IsolationMode.TRANSACTION:
                connection.rollback()
            elif mode is IsolationMode.SCHEMA:
                _reset_schema_sync(connection)
        finally:
            connection.close()


@pytest.fixture
def pglite_execute(pglite_conn: Any) -> Callable[..., list[tuple[Any, ...]]]:
    """A helper that runs SQL and returns rows."""

    def execute(sql: str, params: Sequence[Any] | None = None) -> list[tuple[Any, ...]]:
        with pglite_conn.cursor() as cursor:
            cursor.execute(sql, params)
            if cursor.description is None:
                pglite_conn.commit()
                return []
            rows = list(cursor.fetchall())
        pglite_conn.commit()
        return rows

    return execute


@pytest.fixture
def pglite_engine(pglite_url: str, pglite_server: PGliteServer) -> Iterator[Any]:
    """A SQLAlchemy engine with a freshly isolated schema."""
    sqlalchemy = pytest.importorskip(
        "sqlalchemy", reason="pglite_engine requires pytest-pglite[sqlalchemy]"
    )
    mode = pglite_server.config.isolation
    engine = sqlalchemy.create_engine(pglite_url)
    _reset_engine(
        engine, mode if mode is not IsolationMode.TRANSACTION else IsolationMode.SCHEMA
    )
    try:
        yield engine
    finally:
        try:
            _reset_engine(
                engine,
                mode if mode is not IsolationMode.TRANSACTION else IsolationMode.SCHEMA,
            )
        finally:
            engine.dispose()


try:  # pragma: no cover - depends on the user's environment
    import pytest_asyncio as _pytest_asyncio

    _async_fixture: Callable[[Callable[..., Any]], Any] = _pytest_asyncio.fixture
except ImportError:  # pragma: no cover
    _async_fixture = pytest.fixture


@_async_fixture
async def pglite_async_conn(
    pglite_dsn: str, pglite_server: PGliteServer
) -> AsyncIterator[Any]:
    """A psycopg async connection with a freshly isolated schema."""
    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover
        raise pytest.UsageError(
            "pglite_async_conn requires psycopg; install pytest-pglite[async]"
        ) from exc
    mode = pglite_server.config.isolation
    connection = await psycopg.AsyncConnection.connect(pglite_dsn)
    if mode is IsolationMode.SCHEMA:
        await _reset_schema_async(connection)
    elif mode is IsolationMode.TRANSACTION:
        await connection.rollback()
    try:
        yield connection
    finally:
        try:
            if mode is IsolationMode.TRANSACTION:
                await connection.rollback()
            elif mode is IsolationMode.SCHEMA:
                await _reset_schema_async(connection)
        finally:
            await connection.close()


@_async_fixture
async def pglite_asyncpg(pglite_server: PGliteServer) -> AsyncIterator[Any]:
    """An asyncpg connection to the running server."""
    asyncpg = pytest.importorskip(
        "asyncpg", reason="pglite_asyncpg requires pytest-pglite[examples]"
    )
    kwargs: dict[str, Any] = {
        "user": pglite_server.config.user,
        "password": pglite_server.config.password,
        "database": pglite_server.config.database,
    }
    if pglite_server.port is not None:
        kwargs["host"] = pglite_server.config.tcp_host
        kwargs["port"] = pglite_server.port
    else:
        socket_dir = pglite_server.socket_dir
        assert socket_dir is not None
        kwargs["host"] = str(socket_dir)
        kwargs["port"] = 5432
    connection = await asyncpg.connect(**kwargs, server_settings={})
    mode = pglite_server.config.isolation
    if mode is IsolationMode.SCHEMA:
        for statement in _RESET_STATEMENTS:
            await connection.execute(statement)
    try:
        yield connection
    finally:
        try:
            if mode is IsolationMode.TRANSACTION:
                await connection.execute("ROLLBACK")
            elif mode is IsolationMode.SCHEMA:
                for statement in _RESET_STATEMENTS:
                    await connection.execute(statement)
        finally:
            await connection.close()
