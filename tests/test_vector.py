"""pgvector integration through the statically linked extension."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from pytest_pglite import PGliteConfig, PGliteServer

psycopg = pytest.importorskip("psycopg")

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def server() -> Iterator[PGliteServer]:
    with PGliteServer(PGliteConfig(extensions=("vector",))) as running:
        yield running


def test_vector_is_installed(server: PGliteServer) -> None:
    with psycopg.connect(server.dsn) as conn:
        row = conn.execute(
            "select extversion from pg_extension where extname = 'vector'"
        ).fetchone()
        assert row == ("0.8.0",)
        conn.rollback()


def test_vector_type_and_operators(server: PGliteServer) -> None:
    with psycopg.connect(server.dsn) as conn:
        conn.execute("create table items (id serial primary key, embedding vector(3))")
        conn.execute(
            "insert into items (embedding) values "
            "('[1,2,3]'), ('[4,5,6]'), ('[7,8,9]')"
        )
        nearest = conn.execute(
            "select id from items order by embedding <-> '[1,2,3]'::vector limit 1"
        ).fetchone()
        assert nearest == (1,)
        distance = conn.execute(
            "select '[1,2,3]'::vector <-> '[4,5,6]'::vector"
        ).fetchone()
        assert distance is not None
        assert float(distance[0]) == pytest.approx(5.196152, rel=1e-5)
        conn.rollback()


def test_hnsw_index(server: PGliteServer) -> None:
    with psycopg.connect(server.dsn) as conn:
        conn.execute("create table embeddings (id serial primary key, v vector(4))")
        conn.execute(
            "insert into embeddings (v) select "
            "array[random(), random(), random(), random()]::vector "
            "from generate_series(1, 200)"
        )
        conn.execute("create index on embeddings using hnsw (v vector_l2_ops)")
        conn.execute("set local hnsw.ef_search = 40")
        count = conn.execute(
            "select count(*) from (select id from embeddings "
            "order by v <-> '[0.5,0.5,0.5,0.5]' limit 5) nearest"
        ).fetchone()
        assert count == (5,)
        conn.rollback()


def test_vector_error_recovery(server: PGliteServer) -> None:
    with psycopg.connect(server.dsn) as conn:
        with pytest.raises(psycopg.errors.DataException):
            conn.execute("select '[1,2]'::vector <-> '[1,2,3]'::vector")
        conn.rollback()
        assert conn.execute("select '[1,2,3]'::vector <-> '[1,2,3]'").fetchone() == (
            0.0,
        )
        conn.rollback()
