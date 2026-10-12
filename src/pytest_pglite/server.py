"""High-level server lifecycle for pytest-pglite."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import tempfile
import threading
import weakref
from pathlib import Path
from urllib.parse import quote

from platformdirs import user_cache_dir

from . import aot
from .artifacts import ensure_prefix, prepare_work_dir
from .config import PGliteConfig
from .engine import WasmEngine
from .errors import PGliteConfigurationError, PGliteError
from .wire.server import WireServer

SOCKET_NAME = ".s.PGSQL.5432"
MAX_SOCKET_PATH = 100

#: Servers started in this process.
#:
#: A host may build its own ``PGliteServer`` instead of using the plugin's
#: fixture (the common case), so registration happens in :meth:`PGliteServer.start`
#: and the plugin's lifecycle hooks iterate whatever is running.
_LIVE_SERVERS: weakref.WeakSet[PGliteServer] = weakref.WeakSet()


def live_servers() -> tuple[PGliteServer, ...]:
    """Every PGlite server currently started in this process."""
    return tuple(_LIVE_SERVERS)


def _copy_tree(source: Path, destination: Path) -> None:
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(source, destination, copy_function=shutil.copy2)


def _snapshot_base(base: Path, template: Path) -> None:
    """Atomically copy an initialised data directory into the template cache."""
    template.parent.mkdir(parents=True, exist_ok=True)
    if template.exists():
        return
    tmp = template.with_name(f"{template.name}.tmp{os.getpid()}")
    shutil.rmtree(tmp, ignore_errors=True)
    try:
        shutil.copytree(base, tmp, copy_function=shutil.copy2)
        os.replace(tmp, template)
    except OSError:
        shutil.rmtree(tmp, ignore_errors=True)


class PGliteServer:
    """Runs a PGlite instance and exposes it over the Postgres wire protocol."""

    def __init__(
        self,
        config: PGliteConfig | None = None,
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        self.config = config or PGliteConfig()
        self._log = logger or logging.getLogger("pytest_pglite")
        self._work_dir: Path | None = None
        self._prefix: Path | None = None
        self._artifact_digest: str | None = None
        self._engine: WasmEngine | None = None
        self._wire: WireServer | None = None
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ready = threading.Event()
        self._error: BaseException | None = None
        self._started = False
        self._tests_since_restart = 0
        self._restarts = 0

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        if self._started:
            return
        work_dir = self._make_work_dir()
        prefix, digest = ensure_prefix(self.config.wasm_path)
        self._artifact_digest = digest
        prepare_work_dir(prefix, work_dir)
        engine_prefix = work_dir / "tmp" / "pglite"
        cache_dir = Path(user_cache_dir("pytest-pglite")) / "aot"
        key = aot.cache_key(engine_prefix / "bin" / "pglite.wasi", digest)
        engine = WasmEngine(
            prefix=engine_prefix,
            work_dir=work_dir,
            cache_dir=cache_dir,
            cache_key=key,
            use_aot=self.config.aot_cache,
            user=self.config.user,
            database=self.config.database,
            password=self.config.password,
            log_level=self.config.log_level,
            session_settings=self.config.session_settings,
            memory_reservation_mb=self.config.memory_reservation_mb,
            memory_reservation_for_growth_mb=(
                self.config.memory_reservation_for_growth_mb
            ),
            memory_guard_size_mb=self.config.memory_guard_size_mb,
            memory_may_move=self.config.memory_may_move,
            max_wasm_stack_mb=self.config.max_wasm_stack_mb,
        )
        socket_path: Path | None = None
        if self.config.unix_socket:
            socket_path = work_dir / "sock" / SOCKET_NAME
            if len(str(socket_path)) > MAX_SOCKET_PATH:
                raise PGliteConfigurationError(
                    f"Unix socket path is too long ({socket_path}); "
                    "use a shorter --pglite-work-dir or TCP"
                )
        wire = WireServer(
            engine=engine,
            config=self.config,
            socket_path=socket_path,
            logger=self._log,
        )
        self._work_dir = work_dir
        self._prefix = engine_prefix
        self._engine = engine
        self._wire = wire
        self._ready = threading.Event()
        self._error = None
        self._thread = threading.Thread(
            target=self._thread_main,
            name=f"pglite-{work_dir.name}",
            daemon=True,
        )
        self._thread.start()
        if not self._ready.wait(self.config.startup_timeout):
            self._abort_startup()
            raise PGliteError(
                f"PGlite did not start within {self.config.startup_timeout:.0f}s"
            )
        if self._error is not None:
            error = self._error
            self._abort_startup()
            raise PGliteError(f"PGlite failed to start: {error}") from error
        self._started = True
        _LIVE_SERVERS.add(self)
        self._log.info(
            "PGlite started: heap %.1f MiB of %s%s",
            self.heap_bytes / (1 << 20),
            (
                "unbounded"
                if self.heap_limit_bytes is None
                else f"{self.heap_limit_bytes / (1 << 20):.1f} MiB"
            ),
            self._restart_policy_note(),
        )

    def _restart_policy_note(self) -> str:
        """Describe the restart policy for the startup log line."""
        parts = []
        if self.config.restart_every_n_tests is not None:
            parts.append(f"restart every {self.config.restart_every_n_tests} tests")
        if self.config.heap_restart_mb is not None:
            parts.append(f"restart above {self.config.heap_restart_mb} MiB")
        if not parts:
            parts.append(
                "no restart policy; set restart_every_n_tests or heap_restart_mb"
                " for a long suite"
            )
        return ", ".join(parts)

    def stop(self) -> None:
        _LIVE_SERVERS.discard(self)
        if self._thread is None:
            return
        loop = self._loop
        if loop is not None and loop.is_running():
            future = asyncio.run_coroutine_threadsafe(self._async_stop(), loop)
            with contextlib.suppress(Exception):
                future.result(timeout=30)
            loop.call_soon_threadsafe(loop.stop)
        if self._thread.is_alive():
            self._thread.join(timeout=10)
        self._thread = None
        self._loop = None
        if self._work_dir is not None and not self.config.keep_tmp:
            shutil.rmtree(self._work_dir, ignore_errors=True)
        self._started = False

    def close(self) -> None:
        self.stop()

    def __enter__(self) -> PGliteServer:
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()

    def _abort_startup(self) -> None:
        _LIVE_SERVERS.discard(self)
        loop = self._loop
        if loop is not None and loop.is_running():
            with contextlib.suppress(Exception):
                asyncio.run_coroutine_threadsafe(self._async_stop(), loop).result(
                    timeout=10
                )
            loop.call_soon_threadsafe(loop.stop)
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=5)
        self._thread = None
        if self._work_dir is not None and not self.config.keep_tmp:
            shutil.rmtree(self._work_dir, ignore_errors=True)

    # ------------------------------------------------------------------
    # properties
    # ------------------------------------------------------------------
    @property
    def work_dir(self) -> Path:
        if self._work_dir is None:
            raise PGliteError("server is not started")
        return self._work_dir

    @property
    def socket_path(self) -> Path | None:
        if self._wire is None:
            return None
        return self._wire.socket_path

    @property
    def socket_dir(self) -> Path | None:
        socket = self.socket_path
        return socket.parent if socket is not None else None

    @property
    def port(self) -> int | None:
        if self._wire is None:
            return None
        return self._wire.port

    @property
    def trap_count(self) -> int:
        """Backend traps observed so far.

        The bundled module handles SQL errors with real setjmp/longjmp, so a
        healthy build stays at zero; a positive value means an error aborted
        the WebAssembly instance and the recovery path ran.
        """
        engine = self._engine
        return engine.trap_count if engine is not None else 0

    # ------------------------------------------------------------------
    # guest heap and the restart policy
    # ------------------------------------------------------------------
    @property
    def heap_bytes(self) -> int:
        """Current size of the PGlite guest heap, in bytes.

        This is the number that only ever grows within a session. PGlite runs
        PostgreSQL inside a 32-bit WebAssembly linear memory that can grow but
        never shrink, so a long enough run ratchets this up to the ceiling and
        then fails every allocation. Watch it, and reset it by restarting.
        """
        engine = self._engine
        return engine.heap_bytes if engine is not None else 0

    @property
    def heap_limit_bytes(self) -> int | None:
        """The memory ceiling the artifact declares, or None when unbounded."""
        engine = self._engine
        return engine.heap_limit_bytes if engine is not None else None

    @property
    def restarts(self) -> int:
        """Instance restarts performed for the restart policy."""
        return self._restarts

    @property
    def oom_restarts(self) -> int:
        """Instance restarts performed because the guest heap was exhausted."""
        wire = self._wire
        return wire.oom_restarts if wire is not None else 0

    @property
    def tests_since_restart(self) -> int:
        """Finished tests counted since the last restart."""
        return self._tests_since_restart

    def note_test_finished(self) -> int:
        """Count a finished test for the restart policy; returns the count."""
        self._tests_since_restart += 1
        return self._tests_since_restart

    def restart_due(self) -> str | None:
        """Why the instance should be restarted now, or None.

        Cheap enough to call after every test: a counter, and (when a heap
        threshold is configured) the current guest memory size.
        """
        every = self.config.restart_every_n_tests
        if every is not None and self._tests_since_restart >= every:
            return f"{self._tests_since_restart} tests since the last restart"
        limit_mb = self.config.heap_restart_mb
        if limit_mb is not None and self.heap_bytes >= limit_mb << 20:
            return f"guest heap {self.heap_bytes / (1 << 20):.1f} MiB >= {limit_mb} MiB"
        return None

    def restart(self, reason: str = "requested", *, only_if_idle: bool = True) -> bool:
        """Replace the guest instance, keeping the data directory.

        WebAssembly linear memory never shrinks, so this is how a long session
        gets its heap back, at the cost of a fresh backend session (prepared
        statements and session state are gone; the data is untouched). Safe to
        call from any thread. Returns False when ``only_if_idle`` and a client
        currently holds the backend.
        """
        wire, loop = self._wire, self._loop
        if wire is None or loop is None or not self._started:
            raise PGliteError("server is not started")
        coroutine = (
            wire.restart_if_idle(reason)
            if only_if_idle
            else wire.restart_engine(reason)
        )
        restarted = bool(
            asyncio.run_coroutine_threadsafe(coroutine, loop).result(
                timeout=self.config.startup_timeout
            )
        )
        if restarted:
            self._tests_since_restart = 0
            self._restarts += 1
        return restarted

    def consider_restart(self) -> str | None:
        """Apply the restart policy; returns the reason when it restarted."""
        reason = self.restart_due()
        if reason is None:
            return None
        return reason if self.restart(reason) else None

    @property
    def dsn(self) -> str:
        """A libpq connection string."""
        user = quote(self.config.user, safe="")
        password = quote(self.config.password, safe="")
        database = quote(self.config.database, safe="")
        socket = self.socket_path
        if socket is not None:
            host = quote(str(socket.parent), safe="/")
            return (
                f"postgresql://{user}:{password}@/{database}"
                f"?host={host}&port=5432&sslmode=disable"
            )
        port = self.port or 0
        return (
            f"postgresql://{user}:{password}@{self.config.tcp_host}:{port}"
            f"/{database}?sslmode=disable"
        )

    @property
    def url(self) -> str:
        """A SQLAlchemy URL using the psycopg driver."""
        return self.dsn.replace("postgresql://", "postgresql+psycopg://", 1)

    # ------------------------------------------------------------------
    # queries
    # ------------------------------------------------------------------
    def execute(self, sql: str) -> list[list[str | None]]:
        """Run SQL on the backend, independent of client connections."""
        wire, loop = self._wire, self._loop
        if wire is None or loop is None or not self._started:
            raise PGliteError("server is not started")
        future = asyncio.run_coroutine_threadsafe(wire.execute_system(sql), loop)
        return future.result(timeout=self.config.queue_timeout + 30)

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------
    def _make_work_dir(self) -> Path:
        if self.config.work_dir is not None:
            work_dir = self.config.work_dir
            work_dir.mkdir(parents=True, exist_ok=True)
            if any(work_dir.iterdir()):
                raise PGliteConfigurationError(
                    f"work directory {work_dir} is not empty"
                )
            return work_dir
        return Path(tempfile.mkdtemp(prefix="pytest-pglite-"))

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._async_start())
        except BaseException as exc:  # noqa: BLE001 - reported to start()
            self._error = exc
            self._ready.set()
            return
        self._ready.set()
        try:
            loop.run_forever()
        finally:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True)
                )
            loop.close()

    async def _async_start(self) -> None:
        engine, wire = self._engine, self._wire
        assert engine is not None and wire is not None
        base = self._base_dir()
        template = self._template_path()
        seeded = False
        if template is not None and template.is_dir():
            await asyncio.to_thread(_copy_tree, template, base)
            seeded = True
        try:
            await engine.arun(engine.start)
        except Exception:
            if not seeded:
                raise
            self._log.warning("template database unusable; rebuilding it")
            shutil.rmtree(base, ignore_errors=True)
            await engine.arun(engine.restart)
            seeded = False
        for extension in self.config.extensions:
            await engine.arun(engine.ensure_extension, extension)
        if template is not None and not seeded:
            try:
                await engine.arun(engine.shutdown_instance)
                await asyncio.to_thread(_snapshot_base, base, template)
                await engine.arun(engine.restart)
            except Exception as exc:  # noqa: BLE001 - template is best effort
                self._log.warning("could not create a template database: %s", exc)
                if not engine.started:
                    await engine.arun(engine.restart)
        await wire.start()

    def _base_dir(self) -> Path:
        if self._work_dir is None:
            raise PGliteError("server is not started")
        return self._work_dir / "tmp" / "pglite" / "base"

    def _template_path(self) -> Path | None:
        if (
            not self.config.template_cache
            or self.config.work_dir is not None
            or self._artifact_digest is None
        ):
            return None
        extensions = "_".join(self.config.extensions) or "base"
        key = f"{self._artifact_digest[:16]}-{extensions}"
        return Path(user_cache_dir("pytest-pglite")) / "templates" / key / "base"

    async def _async_stop(self) -> None:
        if self._wire is not None:
            await self._wire.stop()
        if self._engine is not None:
            await self._engine.arun(self._engine.stop)
