"""Wasmtime host for the bundled pglite.wasi module.

The libpglite WASI build exposes a small C-style API. Input is written into
WebAssembly linear memory at address 1 and ``interactive_one`` processes it.
In wire mode the reply is placed at the address returned by ``get_channel``
and its size by ``interactive_read``. Replies larger than the CMA buffer spill
to a socket file inside the preopened ``/tmp`` directory.

SQL errors in the current build abort the WebAssembly instance. The host
recovers by calling ``clear_error`` and flushing with
``interactive_write(-1)``; the ErrorResponse emitted just before the abort is
captured from the flushed reply.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import re
import struct
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

import wasmtime
from wasmtime import Config, Engine, Linker, Store, WasiConfig

from .aot import load_module
from .artifacts import wasm_file
from .errors import (
    PGliteArtifactError,
    PGliteError,
    PGliteTrapError,
    PGliteUnrecoverableError,
)
from .protocol import (
    AUTH_CLEARTEXT,
    AUTH_MD5,
    AUTH_OK,
    BE_AUTHENTICATION,
    build_password,
    build_query,
    build_startup,
    iter_backend_messages,
    md5_password,
    scan_backend,
)

INPUT_ADDRESS = 1
SOCKET_FILE_NAME = ".s.PGSQL.5432.out"

#: 64 KiB, the WebAssembly page size the module uses.
WASM_PAGE_BYTES = 65536

#: Session settings tuned for a 32-bit WebAssembly guest.
#:
#: PostgreSQL's own defaults assume a server with threads and lots of address
#: space. Inside one single-threaded linear memory they only inflate the
#: high-water mark that the instance can never give back: parallel workers
#: cannot run in parallel anyway, a 64 MB maintenance_work_mem is a large
#: transient spike for an index build, and the LLVM JIT has nothing to compile
#: for. Lowering them slows the creep towards the ceiling; it does not raise
#: the ceiling. A host can override or drop any of these with
#: ``session_settings``.
DEFAULT_SESSION_SETTINGS: dict[str, str] = {
    "maintenance_work_mem": "16MB",
    "max_parallel_workers_per_gather": "0",
    "max_parallel_maintenance_workers": "0",
    "jit": "off",
}

_SETTING_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")


def _quote_setting(value: str) -> str:
    """Render a GUC value for a SET statement."""
    return "'" + value.replace("'", "''") + "'"


class WasmEngine:
    """Synchronous engine; all entry points run on a single worker thread."""

    def __init__(
        self,
        *,
        prefix: Path,
        work_dir: Path,
        cache_dir: Path,
        cache_key: str,
        use_aot: bool = True,
        user: str = "postgres",
        database: str = "template1",
        password: str = "password",
        log_level: str = "INFO",
        session_settings: dict[str, str] | None = None,
        memory_reservation_mb: int | None = None,
        memory_reservation_for_growth_mb: int | None = None,
        memory_guard_size_mb: int | None = None,
        memory_may_move: bool | None = None,
        max_wasm_stack_mb: int | None = None,
    ) -> None:
        self._prefix = prefix
        self._work_dir = work_dir
        self._cache_dir = cache_dir
        self._cache_key = cache_key
        self._use_aot = use_aot
        self._user = user
        self._database = database
        self._password = password
        self._log_level = log_level
        self._session_setting_overrides = dict(session_settings or {})
        self._memory_options: dict[str, int | bool] = {}
        if memory_reservation_mb is not None:
            self._memory_options["memory_reservation"] = memory_reservation_mb << 20
        if memory_reservation_for_growth_mb is not None:
            self._memory_options["memory_reservation_for_growth"] = (
                memory_reservation_for_growth_mb << 20
            )
        if memory_guard_size_mb is not None:
            self._memory_options["memory_guard_size"] = memory_guard_size_mb << 20
        if memory_may_move is not None:
            self._memory_options["memory_may_move"] = memory_may_move
        if max_wasm_stack_mb is not None:
            self._memory_options["max_wasm_stack"] = max_wasm_stack_mb << 20
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="pglite-engine"
        )
        self._store: Any = None
        self._exports: Any = None
        self._started = False
        #: Number of backend traps observed; a healthy WASM build with working
        #: setjmp/longjmp stays at zero, because SQL errors do not abort it.
        self.trap_count = 0
        self._last_input_length = 0
        self._wasmtime_engine: Engine | None = None
        self._module: Any = None
        self._log = logging.getLogger("pytest_pglite.engine")

    # ------------------------------------------------------------------
    # executor plumbing
    # ------------------------------------------------------------------
    @property
    def executor(self) -> ThreadPoolExecutor:
        return self._executor

    def submit(self, fn: Callable[..., Any], *args: Any) -> Future[Any]:
        return self._executor.submit(fn, *args)

    async def arun(self, fn: Callable[..., Any], *args: Any) -> Any:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, functools.partial(fn, *args))

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        """Compile, instantiate, initdb and start the backend."""
        if self._started:
            return
        wasm = wasm_file(self._prefix)
        wasmtime_config = Config()
        try:
            wasmtime_config.wasm_exceptions = True
        except AttributeError:  # pragma: no cover - older wasmtime
            pass
        self._apply_memory_options(wasmtime_config)
        engine = Engine(wasmtime_config)
        module = load_module(
            engine, wasm, self._cache_dir, self._cache_key, self._use_aot
        )
        self._wasmtime_engine = engine
        self._module = module
        self._start_instance()

    def _apply_memory_options(self, wasmtime_config: Any) -> None:
        """Apply the host memory configuration, one option at a time.

        Every option is optional and the bindings differ between wasmtime
        releases (``static_memory_maximum_size``, for example, no longer
        exists), so a missing or rejected option is a warning rather than a
        startup failure.
        """
        for name, value in self._memory_options.items():
            setter = getattr(wasmtime_config, name, None)
            if setter is None:
                self._log.warning("wasmtime has no %s option; ignoring %r", name, value)
                continue
            try:
                setter(value)
            except (TypeError, ValueError, AttributeError) as exc:
                self._log.warning(
                    "could not apply wasmtime %s=%r: %s", name, value, exc
                )

    def restart(self) -> None:
        """Replace the backend instance, keeping the data directory."""
        if self._wasmtime_engine is None or self._module is None:
            raise PGliteError("engine was never started")
        self.shutdown_instance()
        self._start_instance()

    def shutdown_instance(self) -> None:
        """Stop the WASM instance (the data directory is kept)."""
        self._drop_instance()

    @property
    def started(self) -> bool:
        return self._started

    @property
    def heap_bytes(self) -> int:
        """Current size of the guest linear memory, in bytes.

        This is the number that only ever grows over a session: it is what
        eventually reaches the 32-bit WebAssembly ceiling (about 4 GiB) and
        makes small allocations fail.
        """
        if self._store is None or self._exports is None:
            return 0
        try:
            return int(self._memory().data_len(self._store))
        except KeyError, TypeError:  # pragma: no cover - defensive
            return 0

    @property
    def heap_limit_bytes(self) -> int | None:
        """The ceiling the module declares for its memory, if any.

        ``None`` means the module declares no maximum, so the ceiling is the
        WebAssembly 32-bit address space rather than a number in the artifact.
        """
        if self._store is None or self._exports is None:
            return None
        try:
            limits = self._memory().type(self._store).limits
        except AttributeError, KeyError, TypeError:  # pragma: no cover
            return None
        if limits.max is None:
            return None
        return int(limits.max) * WASM_PAGE_BYTES

    def ensure_extension(self, name: str) -> None:
        """Create an extension in the shared session database."""
        self._simple(f'CREATE EXTENSION IF NOT EXISTS "{name}"')

    def _start_instance(self) -> None:
        assert self._wasmtime_engine is not None and self._module is not None
        engine = self._wasmtime_engine
        linker = Linker(engine)
        linker.define_wasi()
        store = Store(engine)
        wasi = WasiConfig()
        wasi.argv = ["pglite", "--single", "postgres"]
        wasi.env = [
            ["ENVIRONMENT", "wasi-embed"],
            ["PREFIX", "/tmp/pglite"],
            ["PGDATA", "/tmp/pglite/base"],
            ["PGUSER", self._user],
            ["PGDATABASE", self._database],
            ["PGCLIENTENCODING", "UTF8"],
            ["REPL", "N"],
            ["TZ", "UTC"],
            ["LANG", "C.UTF-8"],
        ]
        wasi.stdout_file = str(self._work_dir / "engine.stdout.log")
        wasi.stderr_file = str(self._work_dir / "engine.stderr.log")
        wasi.preopen_dir(str(self._work_dir / "tmp"), "/tmp")
        wasi.preopen_dir(str(self._work_dir / "dev"), "/dev")
        store.set_wasi(wasi)
        started_at = time.perf_counter()
        try:
            instance = linker.instantiate(store, self._module)
        except wasmtime.WasmtimeError as exc:  # pragma: no cover - bad artifact
            raise PGliteArtifactError(
                f"failed to instantiate {self._prefix}: {exc}"
            ) from exc
        self._store = store
        self._exports = instance.exports(store)
        self._call("_start")
        status = self._call("pgl_initdb")
        if not isinstance(status, int) or not status & 0b110:
            raise PGliteArtifactError(f"pgl_initdb failed with status {status!r}")
        initialized_at = time.perf_counter()
        self._call("pgl_backend")
        self._login()
        self._prepare_session()
        self._started = True
        self._log.debug(
            "engine ready: initdb/resume %.0fms, backend+login %.0fms, "
            "heap %.1f MiB of %s, status %s",
            (initialized_at - started_at) * 1000,
            (time.perf_counter() - initialized_at) * 1000,
            self.heap_bytes / (1 << 20),
            (
                "unbounded"
                if self.heap_limit_bytes is None
                else f"{self.heap_limit_bytes / (1 << 20):.1f} MiB"
            ),
            bin(status),
        )

    def _drop_instance(self) -> None:
        self._started = False
        if self._exports is not None:
            try:
                self._call("pgl_shutdown")
            except PGliteError:
                pass
        self._store = None
        self._exports = None
        self._last_input_length = 0

    def stop(self) -> None:
        self._drop_instance()
        self._executor.shutdown(wait=False, cancel_futures=True)

    def probe(self) -> bool:
        """Return True when the backend session still works."""
        try:
            reply = self.exchange(build_query("SELECT 1"))
        except PGliteError:
            return False
        return not scan_backend(reply).has_error

    # ------------------------------------------------------------------
    # protocol plumbing
    # ------------------------------------------------------------------
    def exchange(self, payload: bytes) -> bytes:
        """Send a complete frontend message stream, return backend bytes."""
        if not payload:
            return b""
        self._last_input_length = len(payload)
        self._call("use_wire", 1)
        self._call("interactive_write", len(payload))
        self._memory().write(self._store, payload, INPUT_ADDRESS)
        self._call("interactive_one")
        return self._read_reply()

    def recover(self) -> bytes:
        """Recover after a trap and flush the pending ErrorResponse."""
        last: PGliteTrapError | None = None
        for _ in range(4):
            try:
                self._call("clear_error")
            except PGliteTrapError as exc:
                last = exc
            try:
                self._call("interactive_write", -1)
                self._call("interactive_one")
            except PGliteTrapError as exc:
                last = exc
                continue
            return self._read_reply()
        raise PGliteUnrecoverableError(f"PGlite backend did not recover: {last}")

    def log_tail(self, lines: int = 30) -> str:
        """Return the tail of the engine stderr log for diagnostics."""
        path = self._work_dir / "engine.stderr.log"
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        return "\n".join(content.splitlines()[-lines:])

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------
    def _memory(self) -> Any:
        return self._exports["memory"]

    def _call(self, name: str, *args: Any) -> Any:
        if self._store is None or self._exports is None:
            raise PGliteError("engine is not started")
        try:
            func = self._exports[name]
        except (KeyError, TypeError) as exc:
            raise PGliteArtifactError(
                f"pglite.wasi does not export {name!r}; wrong artifact version?"
            ) from exc
        try:
            return func(self._store, *args)
        except wasmtime.Trap as exc:
            raise PGliteTrapError(str(exc)) from exc

    def _read_reply(self) -> bytes:
        size = self._call("interactive_read")
        if not size or size < 0:
            return b""
        channel = self._call("get_channel")
        if channel < 0:
            return self._read_socket_file()
        if channel < 2:
            # During trap recovery the C code flushes with the -1 sentinel,
            # which makes get_channel() report 1. The output was written at
            # the same place as always: two bytes past the input message.
            channel = self._last_input_length + 2
        memory = self._memory()
        return bytes(memory.read(self._store, channel, channel + size))

    def _read_socket_file(self) -> bytes:
        path = self._prefix / "base" / SOCKET_FILE_NAME
        if not path.is_file():
            return b""
        data = path.read_bytes()
        path.unlink(missing_ok=True)
        return data

    def _login(self) -> None:
        """Establish the single backend session using the wire protocol."""
        startup = build_startup(
            {
                "user": self._user,
                "database": self._database,
                "client_encoding": "UTF8",
                "application_name": "pytest-pglite",
            }
        )
        reply = self.exchange(startup)
        code, salt = self._auth_request(reply)
        if code == AUTH_MD5:
            assert salt is not None
            response = md5_password(self._password, self._user, salt)
            reply = self.exchange(build_password(response))
            code, _ = self._auth_request(reply)
        elif code == AUTH_CLEARTEXT:
            reply = self.exchange(build_password(self._password))
            code, _ = self._auth_request(reply)
        if code != AUTH_OK:
            raise PGliteArtifactError(
                f"backend authentication failed (request code {code})"
            )

    def _prepare_session(self) -> None:
        """Make the shared session behave like a normal PostgreSQL session."""
        self._simple("CREATE SCHEMA IF NOT EXISTS public")
        self._simple("SET search_path TO public")
        self._simple("SET client_min_messages TO warning")
        self._apply_session_settings()

    def _session_settings(self) -> dict[str, str]:
        """The WASM-tuned defaults with the host's overrides applied.

        A host value of ``''`` drops a default rather than setting it.
        """
        merged = dict(DEFAULT_SESSION_SETTINGS)
        for name, value in self._session_setting_overrides.items():
            if value == "":
                merged.pop(name, None)
            else:
                merged[name] = value
        return merged

    def _apply_session_settings(self) -> None:
        """Apply the session settings, warning on any that this build rejects."""
        for name, value in self._session_settings().items():
            if not _SETTING_NAME_RE.match(name):
                self._log.warning("ignoring invalid session setting name %r", name)
                continue
            try:
                self._simple(f"SET {name} = {_quote_setting(value)}")
            except PGliteError as exc:
                # Tuning must never stop the engine from starting: a custom
                # artifact may be built without a given GUC.
                self._log.warning("could not apply session setting %s: %s", name, exc)

    def _simple(self, sql: str) -> None:
        reply = self.exchange(build_query(sql))
        scan = scan_backend(reply)
        if scan.has_error:
            raise PGliteArtifactError(
                f"session setup failed for {sql!r}: {scan.error_message}"
            )

    @staticmethod
    def _auth_request(reply: bytes) -> tuple[int, bytes | None]:
        for message in iter_backend_messages(reply):
            if message.type != BE_AUTHENTICATION:
                continue
            if len(message.payload) < 4:
                raise PGliteArtifactError("malformed authentication message")
            (code,) = struct.unpack("!I", message.payload[:4])
            salt = message.payload[4:8] if code == AUTH_MD5 else None
            return code, salt
        raise PGliteArtifactError("backend did not send an authentication request")
