"""pg_trgm integration through the statically linked extension."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from pytest_pglite import PGliteConfig, PGliteServer

psycopg = pytest.importorskip("psycopg")

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def server() -> Iterator[PGliteServer]:
    with PGliteServer(PGliteConfig(extensions=("pg_trgm",))) as running:
        yield running


def test_pg_trgm_is_installed(server: PGliteServer) -> None:
    with psycopg.connect(server.dsn) as conn:
        row = conn.execute(
            "select extversion from pg_extension where extname = 'pg_trgm'"
        ).fetchone()
        assert row is not None
        conn.rollback()


def test_similarity_functions_and_operator(server: PGliteServer) -> None:
    with psycopg.connect(server.dsn) as conn:
        similarity = conn.execute(
            "select similarity('hello world', 'hello')"
        ).fetchone()
        assert float(similarity[0]) == pytest.approx(0.5)
        matches = conn.execute(
            "select 'hello' % 'hello world', 'xyz' % 'hello world'"
        ).fetchone()
        assert matches == (True, False)
        word = conn.execute(
            "select word_similarity('hello', 'say hello world')"
        ).fetchone()
        assert float(word[0]) == pytest.approx(1.0)
        conn.rollback()


def test_gin_trigram_index(server: PGliteServer) -> None:
    with psycopg.connect(server.dsn) as conn:
        conn.execute("create table places (id serial primary key, name text)")
        conn.execute(
            "insert into places (name) values "
            "('Acme Corporation'), ('Globex'), ('Initech')"
        )
        conn.execute(
            "create index places_name_trgm on places using gin (name gin_trgm_ops)"
        )
        nearest = conn.execute(
            "select name from places order by name <-> 'acme' limit 1"
        ).fetchone()
        assert nearest == ("Acme Corporation",)
        plan = conn.execute(
            "explain (costs off) select name from places " "where name like '%acme%'"
        ).fetchall()
        assert any("Bitmap Index Scan" in row[0] for row in plan)
        conn.rollback()


def test_pg_trgm_settings(server: PGliteServer) -> None:
    """The _PG_init callback must have registered the GUCs."""
    with psycopg.connect(server.dsn) as conn:
        value = conn.execute("show pg_trgm.similarity_threshold").fetchone()
        assert float(value[0]) == pytest.approx(0.3)
        conn.rollback()


def test_pg_trgm_error_recovery(server: PGliteServer) -> None:
    with psycopg.connect(server.dsn) as conn:
        with pytest.raises(psycopg.errors.UndefinedFunction):
            conn.execute("select similarity('a')")
        conn.rollback()
        assert conn.execute("select similarity('a', 'abc')").fetchone() is not None
        conn.rollback()
