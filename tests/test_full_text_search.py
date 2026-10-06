"""Full-text search through the statically linked snowball dictionaries."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from pytest_pglite import PGliteConfig, PGliteServer

psycopg = pytest.importorskip("psycopg")

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def server() -> Iterator[PGliteServer]:
    with PGliteServer(PGliteConfig()) as running:
        yield running


def test_english_stop_words_and_stemming(server: PGliteServer) -> None:
    with psycopg.connect(server.dsn) as conn:
        row = conn.execute(
            "select to_tsvector('english', 'the running cats')"
        ).fetchone()
        assert row == ("'cat':3 'run':2",)
        conn.rollback()


def test_tsquery_match(server: PGliteServer) -> None:
    with psycopg.connect(server.dsn) as conn:
        conn.execute("create table fts_demo (id int, body text)")
        conn.execute(
            "insert into fts_demo values (1, 'the quick brown fox'), (2, 'lazy dog')"
        )
        conn.execute(
            "create index fts_demo_idx on fts_demo "
            "using gin (to_tsvector('english', body))"
        )
        matched = conn.execute(
            "select id from fts_demo "
            "where to_tsvector('english', body) @@ plainto_tsquery('english', 'fox')"
        ).fetchall()
        assert matched == [(1,)]
        conn.rollback()


def test_generated_tsvector_column(server: PGliteServer) -> None:
    with psycopg.connect(server.dsn) as conn:
        conn.execute(
            "create table fts_generated ("
            "  id int,"
            "  content text,"
            "  tsv tsvector generated always as "
            "    (to_tsvector('english', content)) stored"
            ")"
        )
        conn.execute("insert into fts_generated values (1, 'the running dog')")
        row = conn.execute("select tsv from fts_generated").fetchone()
        assert row == ("'dog':3 'run':2",)
        conn.rollback()
