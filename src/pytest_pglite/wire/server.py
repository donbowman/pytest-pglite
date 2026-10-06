"""Asyncio PostgreSQL wire-protocol server backed by one PGlite instance.

All client connections are logical sessions multiplexed onto a single
single-user Postgres backend. A connection owns the backend for the duration
of an exchange; transactions pin it until they end. This mirrors the model
used by the JavaScript ``@electric-sql/pglite-socket`` package.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import struct
from pathlib import Path

from ..config import PGliteConfig
from ..engine import WasmEngine
from ..errors import (
    PGliteProtocolError,
    PGliteQueueTimeoutError,
    PGliteTrapError,
)
from ..protocol import (
    CANCEL_REQUEST_CODE,
    FE_QUERY,
    FE_SYNC,
    FE_TERMINATE,
    GSSENC_REQUEST_CODE,
    SSL_REQUEST_CODE,
    authentication_ok,
    backend_key_data,
    build_message,
    build_query,
    error_response,
    parameter_status,
    parse_data_rows,
    parse_startup,
    ready_for_query,
    scan_backend,
    strip_ready_for_query,
)
from .state import ConnState, is_copy_from_stdin, rewrite_frontend

_PARAMETER_STATUSES: tuple[tuple[str, str], ...] = (
    ("server_version", "17.5"),
    ("server_encoding", "UTF8"),
    ("client_encoding", "UTF8"),
    ("application_name", ""),
    ("is_superuser", "on"),
    ("session_authorization", "postgres"),
    ("DateStyle", "ISO, MDY"),
    ("IntervalStyle", "postgres"),
    ("TimeZone", "UTC"),
    ("integer_datetimes", "on"),
    ("standard_conforming_strings", "on"),
)

_MAX_MESSAGE_LENGTH = 1 << 30
_MAX_STARTUP_LENGTH = 1 << 20


async def read_frontend_message(
    reader: asyncio.StreamReader,
) -> tuple[bytes, bytes] | None:
    """Read one frontend protocol message, or None on a clean EOF."""
    try:
        header = await reader.readexactly(5)
    except asyncio.IncompleteReadError:
        return None
    mtype = header[:1]
    (length,) = struct.unpack("!I", header[1:])
    if length < 4 or length > _MAX_MESSAGE_LENGTH:
        raise PGliteProtocolError(f"invalid message length {length}")
    try:
        payload = await reader.readexactly(length - 4)
    except asyncio.IncompleteReadError:
        return None
    return mtype, payload


def buffered_message_ready(reader: asyncio.StreamReader) -> bool:
    """True when the reader already holds one complete frontend message.

    ``asyncio.StreamReader`` has no public peek API; the private ``_buffer``
    has been stable for many releases and is the only way to tell whether a
    pipelined batch has arrived without waiting.  When the attribute is
    unavailable this returns False and the caller keeps the one-message-at-a-
    time behaviour.
    """
    buffer = getattr(reader, "_buffer", None)
    if not buffer:
        return False
    data: bytes = bytes(buffer)
    if len(data) < 5:
        return False
    length = int(struct.unpack("!I", data[1:5])[0])
    return 4 <= length <= _MAX_MESSAGE_LENGTH and len(data) >= 1 + length


def buffered_message_type(reader: asyncio.StreamReader) -> bytes | None:
    """Return the type byte of the next buffered message, if any."""
    raw = getattr(reader, "_buffer", None)
    if not raw:
        return None
    data: bytes = bytes(raw)
    return data[:1]


class BackendLease:
    """Single-owner advisory lock around the shared backend session."""

    def __init__(self) -> None:
        self._condition = asyncio.Condition()
        self._owner: ConnState | None = None

    @property
    def owner(self) -> ConnState | None:
        return self._owner

    async def acquire(self, state: ConnState, timeout: float) -> None:
        async with self._condition:
            if self._owner is state:
                return
            try:
                await asyncio.wait_for(
                    self._condition.wait_for(
                        lambda: self._owner is None or self._owner is state
                    ),
                    timeout,
                )
            except TimeoutError:
                owner = self._owner
                detail = f" (held by connection {owner.cid})" if owner else ""
                raise PGliteQueueTimeoutError(
                    "timed out waiting for the PGlite backend" + detail
                ) from None
            self._owner = state

    async def release(self, state: ConnState) -> None:
        async with self._condition:
            if self._owner is state:
                self._owner = None
                self._condition.notify_all()


class WireServer:
    """A PostgreSQL v3 protocol front end for one :class:`WasmEngine`."""

    def __init__(
        self,
        *,
        engine: WasmEngine,
        config: PGliteConfig,
        socket_path: Path | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._engine = engine
        self._config = config
        self._socket_path = socket_path
        self._log = logger or logging.getLogger("pytest_pglite")
        self._servers: list[asyncio.AbstractServer] = []
        self._writers: set[asyncio.StreamWriter] = set()
        self._states: set[ConnState] = set()
        self._lease = BackendLease()
        self._cid = 0
        self._closing = False
        self.port: int | None = None

    @property
    def socket_path(self) -> Path | None:
        return self._socket_path

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    async def start(self) -> None:
        if self._socket_path is not None:
            self._socket_path.parent.mkdir(parents=True, exist_ok=True)
            if self._socket_path.exists():
                self._socket_path.unlink()
            unix_server = await asyncio.start_unix_server(
                self._handle, path=str(self._socket_path)
            )
            self._servers.append(unix_server)
        if self._config.tcp:
            tcp_server = await asyncio.start_server(
                self._handle,
                host=self._config.tcp_host,
                port=self._config.tcp_port,
            )
            self._servers.append(tcp_server)
            sockets = list(tcp_server.sockets or [])
            if sockets:
                self.port = int(sockets[0].getsockname()[1])
        self._log.info(
            "PGlite wire server ready (socket=%s, tcp=%s)",
            self._socket_path,
            self.port,
        )

    async def stop(self) -> None:
        self._closing = True
        for server in self._servers:
            server.close()
        # Close client connections before waiting on the listening servers:
        # since Python 3.13, Server.wait_closed() waits for active connection
        # handlers to finish too, which would deadlock with idle clients.
        for writer in list(self._writers):
            writer.close()
        for writer in list(self._writers):
            with contextlib.suppress(Exception):
                await asyncio.wait_for(writer.wait_closed(), 5)
        for server in self._servers:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(server.wait_closed(), 5)
        self._servers.clear()
        owner = self._lease.owner
        if owner is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._cleanup(owner), 5)
        if self._socket_path is not None and self._socket_path.exists():
            with contextlib.suppress(OSError):
                self._socket_path.unlink()

    # ------------------------------------------------------------------
    # system queries (used by the server for extensions and fixtures)
    # ------------------------------------------------------------------
    async def execute_system(self, sql: str) -> list[list[str | None]]:
        token = ConnState(cid=0)
        await self._lease.acquire(token, self._config.queue_timeout)
        try:
            reply, _ready, _trapped = await self._exchange(token, build_query(sql))
        finally:
            # The system token never continues a transaction, so it always
            # releases the backend when the exchange is done.
            await self._lease.release(token)
        scan = scan_backend(reply)
        if scan.has_error:
            raise PGliteProtocolError(scan.error_message or f"query failed: {sql}")
        return parse_data_rows(reply)

    # ------------------------------------------------------------------
    # connection handling
    # ------------------------------------------------------------------
    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer = writer.get_extra_info("peername") or writer.get_extra_info("sockname")
        state: ConnState | None = None
        try:
            startup = await self._negotiate_startup(reader, writer)
            if startup is None:
                return
            if startup == "cancel":
                self._log.debug("cancel request from %s (ignored)", peer)
                return
            self._cid += 1
            state = ConnState(
                cid=self._cid,
                pid=secrets.randbelow(1 << 31) or 1,
                secret=secrets.randbelow(1 << 31) or 1,
            )
            self._writers.add(writer)
            self._states.add(state)
            writer.write(self._greeting(state))
            await writer.drain()
            await self._serve(state, reader, writer)
        except asyncio.IncompleteReadError:
            pass
        except ConnectionResetError, BrokenPipeError:
            pass
        except PGliteProtocolError as exc:
            self._log.debug("protocol error from %s: %s", peer, exc)
            with contextlib.suppress(Exception):
                writer.write(error_response(str(exc), code="08P01"))
                await writer.drain()
        except PGliteQueueTimeoutError as exc:
            self._log.warning("%s", exc)
            with contextlib.suppress(Exception):
                writer.write(error_response(str(exc), code="57014"))
                await writer.drain()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - defensive
            self._log.exception("unhandled error serving %s: %s", peer, exc)
        finally:
            self._writers.discard(writer)
            if state is not None:
                self._states.discard(state)
                with contextlib.suppress(Exception):
                    await self._cleanup(state)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _negotiate_startup(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> dict[bytes, bytes] | str | None:
        while True:
            try:
                head = await reader.readexactly(4)
            except asyncio.IncompleteReadError:
                return None
            (length,) = struct.unpack("!I", head)
            if length < 8 or length > _MAX_STARTUP_LENGTH:
                raise PGliteProtocolError(f"invalid startup packet length {length}")
            try:
                body = await reader.readexactly(length - 4)
            except asyncio.IncompleteReadError:
                return None
            (code,) = struct.unpack("!I", body[:4])
            if code in (SSL_REQUEST_CODE, GSSENC_REQUEST_CODE):
                writer.write(b"N")
                await writer.drain()
                continue
            if code == CANCEL_REQUEST_CODE:
                return "cancel"
            try:
                return parse_startup(body)
            except PGliteProtocolError as exc:
                writer.write(error_response(str(exc), code="08P01"))
                await writer.drain()
                return None

    def _greeting(self, state: ConnState) -> bytes:
        parts = [authentication_ok()]
        for name, value in _PARAMETER_STATUSES:
            parts.append(parameter_status(name, value))
        parts.append(backend_key_data(state.pid, state.secret))
        parts.append(ready_for_query("I"))
        return b"".join(parts)

    async def _serve(
        self,
        state: ConnState,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        while not self._closing:
            message = await read_frontend_message(reader)
            if message is None:
                return
            mtype, payload = message
            if mtype == FE_TERMINATE:
                return
            self._log.debug("c%d <- %s", state.cid, mtype.decode("ascii", "replace"))
            if mtype == FE_QUERY and is_copy_from_stdin(payload):
                # libpglite's synchronous CMA transport cannot feed CopyData
                # concurrently with CopyFrom, so the backend aborts on the
                # command itself. Answer like a server that lacks the feature.
                status = "E" if state.txn_status == "T" else "I"
                state.txn_status = "E" if status == "E" else state.txn_status
                await self._write(
                    writer,
                    error_response(
                        "COPY FROM STDIN is not supported by the embedded "
                        "PGlite backend",
                        code="0A000",
                    )
                    + ready_for_query(status),
                )
                continue
            outbound = rewrite_frontend(state, mtype, payload)
            await self._lease.acquire(state, self._config.queue_timeout)
            if not await self._converse(state, reader, writer, outbound):
                return

    async def _coalesce_pipeline(
        self,
        state: ConnState,
        reader: asyncio.StreamReader,
        pending: bytes,
    ) -> bytes:
        """Append pipelined messages that have already arrived.

        Extended-protocol clients (psycopg3, asyncpg, SQLAlchemy) send
        Parse/Bind/Describe/Execute/Sync back to back in one write.  Driving
        the Wasmtime backend once per message costs a round trip each;
        coalescing the messages already buffered into a single exchange cuts
        a typical parameterless query from five backend calls to one.  Never
        blocks: it only consumes messages already present in the stream
        buffer, so a client that sends a pipeline in pieces still works.
        """
        if not state.pipeline_open:
            return pending
        parts = [pending]
        while state.pipeline_open and buffered_message_ready(reader):
            if buffered_message_type(reader) == FE_TERMINATE:
                # Leave it for _serve; the batch is the data to process now.
                break
            message = await read_frontend_message(reader)
            if message is None:
                break
            mtype, payload = message
            self._log.debug(
                "c%d <- %s (coalesced)", state.cid, mtype.decode("ascii", "replace")
            )
            parts.append(rewrite_frontend(state, mtype, payload))
        return b"".join(parts)

    async def _converse(
        self,
        state: ConnState,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        pending: bytes,
    ) -> bool:
        """Feed messages to the backend until the exchange completes."""
        while True:
            pending = await self._coalesce_pipeline(state, reader, pending)
            reply, ready, _trapped = await self._exchange(state, pending)
            if reply:
                writer.write(reply)
                await writer.drain()
            if ready is not None:
                state.txn_status = ready
                if ready == "I":
                    await self._lease.release(state)
                return True
            message = await self._next_message(reader)
            if message is None:
                return False
            mtype, payload = message
            if mtype == FE_TERMINATE:
                return False
            self._log.debug(
                "c%d <- %s (pipeline)", state.cid, mtype.decode("ascii", "replace")
            )
            pending = rewrite_frontend(state, mtype, payload)

    async def _next_message(
        self, reader: asyncio.StreamReader
    ) -> tuple[bytes, bytes] | None:
        timeout = self._config.holder_timeout
        if timeout is None:
            return await read_frontend_message(reader)
        try:
            return await asyncio.wait_for(read_frontend_message(reader), timeout)
        except TimeoutError:
            raise PGliteQueueTimeoutError(
                "connection idle while holding the PGlite backend"
            ) from None

    async def _write(self, writer: asyncio.StreamWriter, data: bytes) -> None:
        writer.write(data)
        await writer.drain()

    async def _restart_engine(self, reason: str) -> None:
        self._log.warning("%s; restarting the PGlite instance", reason)
        await self._engine.arun(self._engine.restart)
        for known in self._states:
            known.reset_after_restart()

    async def _exchange(
        self, state: ConnState, payload: bytes
    ) -> tuple[bytes, str | None, bool]:
        trapped = False
        trap_note = ""
        pipeline_open = state.pipeline_open
        try:
            reply = await self._engine.arun(self._engine.exchange, payload)
        except PGliteTrapError as exc:
            trapped = True
            trap_note = str(exc)
            first_line = trap_note.strip().splitlines()[0] if trap_note.strip() else ""
            self._log.debug("PGlite backend trap: %s", first_line)
            state.on_error()
            reply = await self._engine.arun(self._engine.recover)
            try:
                recovered_has_error = scan_backend(reply).has_error
            except PGliteProtocolError:
                recovered_has_error = True
                reply = b""
            if not recovered_has_error:
                reply = error_response(f"PGlite backend error: {trap_note}") + reply
            if not await self._engine.arun(self._engine.probe):
                await self._restart_engine("backend wedged after a trap")
        try:
            scan = scan_backend(reply)
        except PGliteProtocolError:
            # A FATAL abort can leave a partial message in the transport. The
            # session is not trustworthy any more; restart over the same data.
            await self._restart_engine("malformed backend reply")
            reply = b""
            scan = scan_backend(b"")
            trapped = True
        ready = scan.ready_status
        if trapped and pipeline_open and ready is not None:
            # libpglite flushed ReadyForQuery while the client's extended
            # protocol pipeline is still waiting for Sync. Real PostgreSQL
            # only sends it after Sync, so hold it back.
            reply = strip_ready_for_query(reply)
            ready = None
            scan = scan_backend(reply)
        if trapped and not scan.has_error:
            message = (
                f"PGlite backend error: {trap_note}"
                if trap_note
                else "PGlite backend error"
            )
            if pipeline_open:
                reply = error_response(message) + reply
            else:
                reply = error_response(message) + reply + ready_for_query("I")
                ready = "I"
        if self._log.isEnabledFor(logging.DEBUG):
            types = "".join(
                m.type.decode("ascii", "replace") for m in scan_backend(reply).messages
            )
            self._log.debug(
                "c%d -> [%s] ready=%s trapped=%s pipeline=%s",
                state.cid,
                types,
                ready,
                trapped,
                state.pipeline_open,
            )
        if ready is not None:
            state.txn_status = ready
            state.pipeline_open = False
        return reply, ready, trapped

    async def _cleanup(self, state: ConnState) -> None:
        """Release the backend, rolling back an open transaction first."""
        if self._lease.owner is not state:
            return
        if state.txn_status != "I":
            payload = (
                build_message(FE_SYNC, b"")
                + build_query("ROLLBACK")
                + build_message(FE_SYNC, b"")
            )
            with contextlib.suppress(Exception):
                await self._exchange(state, payload)
            state.txn_status = "I"
        await self._lease.release(state)
