"""pgvector on PGlite, with a standalone server configured for the extension."""

from __future__ import annotations

from collections.abc import Iterator

import psycopg
import pytest

from pytest_pglite import PGliteConfig, PGliteServer


@pytest.fixture(scope="module")
def vector_server() -> Iterator[PGliteServer]:
    with PGliteServer(PGliteConfig(extensions=("vector",))) as server:
        yield server


def test_similarity_search(vector_server: PGliteServer) -> None:
    with psycopg.connect(vector_server.dsn) as conn:
        conn.execute(
            "create table documents (id serial primary key, embedding vector(3))"
        )
        conn.execute(
            "insert into documents (embedding) values"
            " ('[0.1, 0.2, 0.3]'), ('[0.9, 0.8, 0.7]')"
        )
        row = conn.execute(
            "select id from documents order by embedding <-> '[0.1, 0.2, 0.3]' limit 1"
        ).fetchone()
        assert row == (1,)
        conn.rollback()


def test_hnsw_index_and_guc(vector_server: PGliteServer) -> None:
    with psycopg.connect(vector_server.dsn) as conn:
        conn.execute("create table items (id serial primary key, v vector(4))")
        conn.execute(
            "insert into items (v) select"
            " array[random(), random(), random(), random()]::vector"
            " from generate_series(1, 100)"
        )
        conn.execute("create index on items using hnsw (v vector_l2_ops)")
        conn.execute("set local hnsw.ef_search = 32")
        count = conn.execute(
            "select count(*) from (select id from items"
            " order by v <-> '[0.5,0.5,0.5,0.5]' limit 3) nearest"
        ).fetchone()
        assert count == (3,)
        conn.rollback()
