"""Unit tests for the wire-protocol codec."""

from __future__ import annotations

import hashlib
import struct

import pytest

from pytest_pglite.errors import PGliteProtocolError
from pytest_pglite.protocol import (
    FE_QUERY,
    build_message,
    build_password,
    build_query,
    build_startup,
    encode_cstring,
    error_response,
    iter_backend_messages,
    md5_password,
    parameter_status,
    parse_bind,
    parse_close,
    parse_data_rows,
    parse_describe,
    parse_parse,
    parse_query,
    parse_startup,
    ready_for_query,
    scan_backend,
)


def test_query_roundtrip() -> None:
    message = build_query("select 1")
    assert message[:1] == FE_QUERY
    (length,) = struct.unpack("!I", message[1:5])
    assert length == len(message) - 1
    assert parse_query(message[5:]) == b"select 1"


def test_startup_roundtrip() -> None:
    message = build_startup({"user": "postgres", "database": "template1"})
    params = parse_startup(message[4:])
    assert params == {b"user": b"postgres", b"database": b"template1"}


def test_parse_and_bind_fields() -> None:
    payload = (
        encode_cstring("stmt") + encode_cstring("select $1") + struct.pack("!H", 0)
    )
    name, query, rest = parse_parse(payload)
    assert name == b"stmt"
    assert query == b"select $1"
    assert rest == struct.pack("!H", 0)

    bind_payload = (
        encode_cstring("portal")
        + encode_cstring("stmt")
        + struct.pack("!H", 0)
        + struct.pack("!H", 0)
        + struct.pack("!H", 0)
    )
    portal, statement, rest = parse_bind(bind_payload)
    assert portal == b"portal"
    assert statement == b"stmt"
    assert rest

    describe_payload = b"S" + encode_cstring("stmt")
    kind, name = parse_describe(describe_payload)
    assert kind == b"S"
    assert name == b"stmt"

    close_payload = b"P" + encode_cstring("portal")
    kind, name = parse_close(close_payload)
    assert kind == b"P"
    assert name == b"portal"


def test_cstring_rejects_nul() -> None:
    with pytest.raises(PGliteProtocolError):
        encode_cstring("bad\x00value")


def test_md5_password_matches_postgres_algorithm() -> None:
    salt = bytes([0x01, 0x02, 0x03, 0x04])
    inner = hashlib.md5(b"secretpostgres").hexdigest().encode()
    expected = "md5" + hashlib.md5(inner + salt).hexdigest()
    assert md5_password("secret", "postgres", salt) == expected
    assert len(md5_password("secret", "postgres", salt)) == 35


def test_password_message() -> None:
    message = build_password("md5abc")
    assert message[:1] == b"p"
    assert message[5:-1] == b"md5abc"


def test_scan_backend_finds_ready_and_error() -> None:
    stream = (
        parameter_status("server_version", "17.5")
        + error_response("boom", code="42P01")
        + ready_for_query("I")
    )
    scan = scan_backend(stream)
    assert scan.ready_status == "I"
    assert scan.has_error is True
    assert scan.error_message == "boom"
    assert scan.parameter_statuses == [("server_version", "17.5")]
    messages = iter_backend_messages(stream)
    assert len(messages) == 3


def test_scan_backend_rejects_truncated() -> None:
    with pytest.raises(PGliteProtocolError):
        scan_backend(b"Z\x00\x00\x00\x05")


def test_parse_data_rows() -> None:
    payload = struct.pack("!H", 2)
    payload += struct.pack("!i", 1) + b"1"
    payload += struct.pack("!i", -1)
    stream = build_message(b"D", payload) + ready_for_query("I")
    assert parse_data_rows(stream) == [["1", None]]


def test_build_message_length_includes_itself() -> None:
    message = build_message(b"X", b"abc")
    (length,) = struct.unpack("!I", message[1:5])
    assert length == 7
