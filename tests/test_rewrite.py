"""Tests for the CONCURRENTLY compatibility rewrite."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from pytest_pglite import PGliteConfig, PGliteServer
from pytest_pglite.protocol import (
    FE_PARSE,
    FE_QUERY,
    encode_cstring,
    parse_parse,
    parse_query,
)
from pytest_pglite.wire.rewrite import rewrite_concurrent_index
from pytest_pglite.wire.state import ConnState, rewrite_frontend

psycopg = pytest.importorskip("psycopg")


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        (b"create index i on t (a)", b"create index i on t (a)"),
        (
            b"create index concurrently i on t (a)",
            b"create index i on t (a)",
        ),
        (
            b"CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS i ON t (a)",
            b"CREATE UNIQUE INDEX IF NOT EXISTS i ON t (a)",
        ),
        (b"drop index concurrently if exists i", b"drop index if exists i"),
        (b"reindex index concurrently i", b"reindex index i"),
        (
            b"reindex (verbose) table concurrently t",
            b"reindex (verbose) table t",
        ),
        (
            b"/* comment */ create index concurrently i on t (a)",
            b"/* comment */ create index i on t (a)",
        ),
        (
            b"select ';'; create index concurrently i on t (a); select 2",
            b"select ';'; create index i on t (a); select 2",
        ),
        (
            b"select 'create index concurrently i on t (a)'",
            b"select 'create index concurrently i on t (a)'",
        ),
        (
            b"select $$create index concurrently i$$",
            b"select $$create index concurrently i$$",
        ),
        (
            b"create table x (a text default 'drop index concurrently')",
            b"create table x (a text default 'drop index concurrently')",
        ),
        (
            b"   \n\tcreate index concurrently i on t (a)",
            b"   \n\tcreate index i on t (a)",
        ),
    ],
)
def test_rewrite_concurrent_index(sql: bytes, expected: bytes) -> None:
    assert rewrite_concurrent_index(sql) == expected


def test_simple_query_message_is_rewritten() -> None:
    state = ConnState(cid=1)
    payload = encode_cstring(b"create index concurrently i on t (a)")
    message = rewrite_frontend(state, FE_QUERY, payload)
    assert parse_query(message[5:]) == b"create index i on t (a)"


def test_parse_message_is_rewritten() -> None:
    state = ConnState(cid=1)
    payload = encode_cstring(b"s1") + encode_cstring(b"drop index concurrently i")
    message = rewrite_frontend(state, FE_PARSE, payload)
    name, query, _rest = parse_parse(message[5:])
    assert name.startswith(b"pp")
    assert query == b"drop index i"


def test_rewrite_can_be_disabled() -> None:
    state = ConnState(cid=1, rewrite_concurrent_index=False)
    payload = encode_cstring(b"create index concurrently i on t (a)")
    message = rewrite_frontend(state, FE_QUERY, payload)
    assert parse_query(message[5:]) == b"create index concurrently i on t (a)"


@pytest.fixture(scope="module")
def server() -> Iterator[PGliteServer]:
    with PGliteServer(PGliteConfig()) as running:
        yield running


def test_create_index_concurrently(server: PGliteServer) -> None:
    with psycopg.connect(server.dsn, autocommit=True) as conn:
        conn.execute("create table cic_demo (id int, name text)")
        conn.execute(
            "insert into cic_demo select g, 'row-' || g "
            "from generate_series(1, 100) g"
        )
        conn.execute("create index concurrently cic_demo_name_idx on cic_demo (name)")
        valid = conn.execute(
            "select indisvalid from pg_index "
            "where indexrelid = 'cic_demo_name_idx'::regclass"
        ).fetchone()
        assert valid == (True,)
        conn.execute("drop index concurrently cic_demo_name_idx")
        dropped = conn.execute("select to_regclass('cic_demo_name_idx')").fetchone()
        assert dropped == (None,)


def test_create_index_concurrently_if_not_exists(server: PGliteServer) -> None:
    with psycopg.connect(server.dsn, autocommit=True) as conn:
        conn.execute("create table cic_if_not_exists (id int)")
        conn.execute(
            "create index if not exists cic_if_exists_idx " "on cic_if_not_exists (id)"
        )
        conn.execute(
            "create index concurrently if not exists cic_if_exists_idx "
            "on cic_if_not_exists (id)"
        )
        valid = conn.execute(
            "select indisvalid from pg_index "
            "where indexrelid = 'cic_if_exists_idx'::regclass"
        ).fetchone()
        assert valid == (True,)


def test_rewrite_disabled_still_fails() -> None:
    with PGliteServer(PGliteConfig(rewrite_concurrent_index=False)) as raw:
        with psycopg.connect(raw.dsn, autocommit=True) as conn:
            conn.execute("create table cic_raw (id int, name text)")
            with pytest.raises(psycopg.errors.InternalError_):
                conn.execute(
                    "create index concurrently cic_raw_name_idx " "on cic_raw (name)"
                )
