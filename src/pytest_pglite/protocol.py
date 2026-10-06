"""Codec for the PostgreSQL v3 frontend/backend protocol.

pytest-pglite terminates client sessions and drives a single libpglite
backend session. Only the message types needed to multiplex sessions are
parsed in detail; everything else is forwarded verbatim.
"""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass, field

from .errors import PGliteProtocolError

PROTOCOL_VERSION_30 = 196608
SSL_REQUEST_CODE = 80877103
GSSENC_REQUEST_CODE = 80877104
CANCEL_REQUEST_CODE = 80877102

# Frontend message types
FE_QUERY = b"Q"
FE_PARSE = b"P"
FE_BIND = b"B"
FE_DESCRIBE = b"D"
FE_EXECUTE = b"E"
FE_SYNC = b"S"
FE_FLUSH = b"H"
FE_CLOSE = b"C"
FE_TERMINATE = b"X"
FE_PASSWORD = b"p"
FE_FUNCTION_CALL = b"F"
FE_COPY_DATA = b"d"
FE_COPY_DONE = b"c"
FE_COPY_FAIL = b"f"

# Backend message types
BE_AUTHENTICATION = b"R"
BE_PARAMETER_STATUS = b"S"
BE_BACKEND_KEY_DATA = b"K"
BE_READY_FOR_QUERY = b"Z"
BE_ERROR_RESPONSE = b"E"
BE_NOTICE_RESPONSE = b"N"
BE_PARSE_COMPLETE = b"1"
BE_BIND_COMPLETE = b"2"
BE_CLOSE_COMPLETE = b"3"
BE_COMMAND_COMPLETE = b"C"
BE_ROW_DESCRIPTION = b"T"
BE_DATA_ROW = b"D"
BE_EMPTY_QUERY = b"I"
BE_NO_DATA = b"n"
BE_PORTAL_SUSPENDED = b"s"
BE_PARAMETER_DESCRIPTION = b"t"
BE_COPY_IN_RESPONSE = b"G"
BE_COPY_OUT_RESPONSE = b"H"
BE_COPY_BOTH_RESPONSE = b"W"
BE_COPY_DATA = b"d"
BE_COPY_DONE = b"c"
BE_NOTIFICATION = b"A"
BE_FUNCTION_RESULT = b"V"

# Authentication request codes
AUTH_OK = 0
AUTH_CLEARTEXT = 3
AUTH_MD5 = 5
AUTH_SASL = 10


def encode_cstring(value: str | bytes) -> bytes:
    """Encode a NUL-terminated string."""
    data = value.encode("utf-8") if isinstance(value, str) else value
    if b"\x00" in data:
        raise PGliteProtocolError("cstring must not contain NUL bytes")
    return data + b"\x00"


def read_cstring(buf: bytes, offset: int) -> tuple[bytes, int]:
    """Read a NUL-terminated string, returning (value, next_offset)."""
    end = buf.find(b"\x00", offset)
    if end < 0:
        raise PGliteProtocolError("unterminated cstring in protocol message")
    return buf[offset:end], end + 1


def build_message(type_byte: bytes, payload: bytes) -> bytes:
    """Frame a protocol message."""
    return type_byte + struct.pack("!I", len(payload) + 4) + payload


def build_query(sql: str) -> bytes:
    """A simple-query (``Q``) message."""
    return build_message(FE_QUERY, encode_cstring(sql))


def build_startup(params: dict[str, str]) -> bytes:
    """A startup message."""
    payload = struct.pack("!I", PROTOCOL_VERSION_30)
    for key, value in params.items():
        payload += encode_cstring(key) + encode_cstring(value)
    payload += b"\x00"
    return struct.pack("!I", len(payload) + 4) + payload


def build_password(password: str) -> bytes:
    """A password (``p``) message (cleartext or md5)."""
    return build_message(FE_PASSWORD, encode_cstring(password))


def md5_password(password: str, user: str, salt: bytes) -> str:
    """Compute the Postgres md5 password response."""
    inner = hashlib.md5(password.encode("utf-8") + user.encode("utf-8"))
    outer = hashlib.md5(inner.hexdigest().encode("ascii") + salt)
    return "md5" + outer.hexdigest()


def authentication_ok() -> bytes:
    return build_message(BE_AUTHENTICATION, struct.pack("!I", AUTH_OK))


def parameter_status(name: str, value: str) -> bytes:
    return build_message(
        BE_PARAMETER_STATUS, encode_cstring(name) + encode_cstring(value)
    )


def backend_key_data(pid: int, secret: int) -> bytes:
    return build_message(BE_BACKEND_KEY_DATA, struct.pack("!II", pid, secret))


def ready_for_query(status: str = "I") -> bytes:
    if status not in {"I", "T", "E"}:
        raise ValueError(f"invalid transaction status {status!r}")
    return build_message(BE_READY_FOR_QUERY, status.encode("ascii"))


def error_response(
    message: str,
    *,
    severity: str = "ERROR",
    code: str = "XX000",
    detail: str | None = None,
    hint: str | None = None,
) -> bytes:
    """Build an ErrorResponse message."""
    fields = [
        (b"S", severity),
        (b"V", severity),
        (b"C", code),
        (b"M", message),
    ]
    if detail:
        fields.append((b"D", detail))
    if hint:
        fields.append((b"H", hint))
    payload = b"".join(tag + encode_cstring(value) for tag, value in fields) + b"\x00"
    return build_message(BE_ERROR_RESPONSE, payload)


def notice_response(
    message: str, *, severity: str = "NOTICE", code: str = "00000"
) -> bytes:
    fields = [
        (b"S", severity),
        (b"V", severity),
        (b"C", code),
        (b"M", message),
    ]
    payload = b"".join(tag + encode_cstring(value) for tag, value in fields) + b"\x00"
    return build_message(BE_NOTICE_RESPONSE, payload)


def parse_startup(payload: bytes) -> dict[bytes, bytes]:
    """Parse the body of a startup message into key/value pairs."""
    if len(payload) < 4:
        raise PGliteProtocolError("startup packet too short")
    code = struct.unpack("!I", payload[:4])[0]
    params: dict[bytes, bytes] = {}
    offset = 4
    while offset < len(payload):
        if payload[offset] == 0:
            break
        key, offset = read_cstring(payload, offset)
        value, offset = read_cstring(payload, offset)
        params[key] = value
    if code not in {
        PROTOCOL_VERSION_30,
        PROTOCOL_VERSION_30 + 1,
        PROTOCOL_VERSION_30 + 2,
    }:
        raise PGliteProtocolError(f"unsupported protocol version {code}")
    return params


def parse_parse(payload: bytes) -> tuple[bytes, bytes, bytes]:
    """Parse a Parse message into (statement_name, query, remainder)."""
    name, offset = read_cstring(payload, 0)
    query, offset = read_cstring(payload, offset)
    return name, query, payload[offset:]


def parse_bind(payload: bytes) -> tuple[bytes, bytes, bytes]:
    """Parse a Bind message into (portal_name, statement_name, remainder)."""
    portal, offset = read_cstring(payload, 0)
    statement, offset = read_cstring(payload, offset)
    return portal, statement, payload[offset:]


def parse_describe(payload: bytes) -> tuple[bytes, bytes]:
    """Parse a Describe message into (target_kind, name)."""
    if len(payload) < 2:
        raise PGliteProtocolError("Describe message too short")
    name, _ = read_cstring(payload, 1)
    return payload[:1], name


def parse_close(payload: bytes) -> tuple[bytes, bytes]:
    """Parse a Close message into (target_kind, name)."""
    if len(payload) < 2:
        raise PGliteProtocolError("Close message too short")
    name, _ = read_cstring(payload, 1)
    return payload[:1], name


def parse_execute(payload: bytes) -> tuple[bytes, bytes]:
    """Parse an Execute message into (portal_name, remainder)."""
    portal, offset = read_cstring(payload, 0)
    return portal, payload[offset:]


def parse_query(payload: bytes) -> bytes:
    """Return the SQL of a simple-query message."""
    sql, _ = read_cstring(payload, 0)
    return sql


@dataclass(frozen=True)
class BackendMessage:
    type: bytes
    payload: bytes


@dataclass
class BackendScan:
    """Summary of a backend response stream."""

    messages: list[BackendMessage] = field(default_factory=list)
    ready_status: str | None = None
    has_error: bool = False
    error_message: str | None = None
    parameter_statuses: list[tuple[str, str]] = field(default_factory=list)


def iter_backend_messages(data: bytes) -> list[BackendMessage]:
    """Split a backend response byte stream into messages."""
    messages: list[BackendMessage] = []
    offset = 0
    total = len(data)
    while offset < total:
        if offset + 5 > total:
            raise PGliteProtocolError("truncated backend message header")
        type_byte = data[offset : offset + 1]
        (length,) = struct.unpack("!I", data[offset + 1 : offset + 5])
        if length < 4 or offset + 1 + length > total:
            raise PGliteProtocolError("truncated backend message body")
        payload = data[offset + 5 : offset + 1 + length]
        messages.append(BackendMessage(type_byte, payload))
        offset += 1 + length
    return messages


def scan_backend(data: bytes) -> BackendScan:
    """Summarise a backend response stream."""
    scan = BackendScan()
    for message in iter_backend_messages(data):
        scan.messages.append(message)
        if message.type == BE_READY_FOR_QUERY and message.payload:
            scan.ready_status = message.payload[:1].decode("ascii", "replace")
        elif message.type == BE_ERROR_RESPONSE:
            scan.has_error = True
            scan.error_message = parse_error_fields(message.payload)
        elif message.type == BE_PARAMETER_STATUS:
            name, offset = read_cstring(message.payload, 0)
            value, _ = read_cstring(message.payload, offset)
            scan.parameter_statuses.append(
                (name.decode("utf-8", "replace"), value.decode("utf-8", "replace"))
            )
    return scan


def strip_ready_for_query(data: bytes) -> bytes:
    """Remove ReadyForQuery messages from a backend byte stream.

    Used after trap recovery: libpglite emits ReadyForQuery even while an
    extended-protocol pipeline is still waiting for the client's Sync, but
    real clients must only see it once Sync has been processed.
    """
    if not data:
        return data
    result = bytearray()
    for message in iter_backend_messages(data):
        if message.type == BE_READY_FOR_QUERY:
            continue
        result += build_message(message.type, message.payload)
    return bytes(result)


def parse_error_fields(payload: bytes) -> str | None:
    """Extract the message field from an ErrorResponse payload."""
    offset = 0
    while offset < len(payload):
        if payload[offset] == 0:
            break
        tag = payload[offset : offset + 1]
        value, offset = read_cstring(payload, offset + 1)
        if tag == b"M":
            return value.decode("utf-8", "replace")
    return None


def parse_data_rows(data: bytes) -> list[list[str | None]]:
    """Decode text-format DataRow messages into Python strings."""
    rows: list[list[str | None]] = []
    for message in iter_backend_messages(data):
        if message.type != BE_DATA_ROW:
            continue
        payload = message.payload
        if len(payload) < 2:
            continue
        (count,) = struct.unpack("!H", payload[:2])
        offset = 2
        row: list[str | None] = []
        for _ in range(count):
            (length,) = struct.unpack("!i", payload[offset : offset + 4])
            offset += 4
            if length < 0:
                row.append(None)
            else:
                value = payload[offset : offset + length]
                offset += length
                row.append(value.decode("utf-8", "replace"))
        rows.append(row)
    return rows
