"""Every bundled contrib extension can be created and used."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from pytest_pglite import PGliteConfig, PGliteServer

psycopg = pytest.importorskip("psycopg")

pytestmark = pytest.mark.integration

EXTENSIONS = (
    "bloom",
    "btree_gin",
    "btree_gist",
    "citext",
    "cube",
    "dict_int",
    "earthdistance",
    "fuzzystrmatch",
    "hstore",
    "intarray",
    "isn",
    "ltree",
    "pg_trgm",
    "seg",
    "tablefunc",
    "tsm_system_rows",
    "tsm_system_time",
    "unaccent",
)


@pytest.fixture(scope="module")
def server() -> Iterator[PGliteServer]:
    # Create the extensions in the fixture so every test works regardless of
    # the order pytest-xdist runs them in.
    with PGliteServer(PGliteConfig(extensions=EXTENSIONS)) as running:
        yield running


def test_all_extensions_install(server: PGliteServer) -> None:
    with psycopg.connect(server.dsn, autocommit=True) as conn:
        for name in EXTENSIONS:
            conn.execute(f"create extension if not exists {name}")
        installed = {row[0] for row in conn.execute("select extname from pg_extension")}
        assert set(EXTENSIONS) <= installed


def test_extension_features(server: PGliteServer) -> None:
    with psycopg.connect(server.dsn, autocommit=True) as conn:
        assert conn.execute("select 'Hello'::citext = 'hello'::citext").fetchone() == (
            True,
        )

        assert conn.execute("select 'a=>1'::hstore -> 'a'").fetchone() == ("1",)
        assert conn.execute("select levenshtein('kitten', 'sitting')").fetchone() == (
            3,
        )
        assert conn.execute("select similarity('hello', 'hallo') > 0.3").fetchone() == (
            True,
        )
        assert conn.execute("select unaccent('Hôtel')").fetchone() == ("Hotel",)
        assert conn.execute("select '{1,2,3}'::int[] && '{2}'::int[]").fetchone() == (
            True,
        )
        assert conn.execute("select nlevel('a.b.c'::ltree)").fetchone() == (3,)
        assert conn.execute("select '(1,2)'::cube <-> '(1,2)'::cube").fetchone() == (
            0.0,
        )
        assert conn.execute(
            "select earth_distance(ll_to_earth(0, 0), ll_to_earth(0, 0))"
        ).fetchone() == (0.0,)
        assert conn.execute("select '1..10'::seg @> '5'::seg").fetchone() == (True,)
        assert conn.execute(
            "select count(*) from normal_rand(5, 0::float8, 1::float8)"
        ).fetchone() == (5,)
        conn.execute("create table tsm_demo (a int)")
        conn.execute("insert into tsm_demo select g from generate_series(1, 100) g")
        assert conn.execute(
            "select count(*) from tsm_demo tablesample system_rows(3)"
        ).fetchone() == (3,)
        timed = conn.execute(
            "select count(*) from tsm_demo tablesample system_time(1)"
        ).fetchone()
        assert timed is not None
        assert conn.execute("select '0-596-00289-0'::isbn").fetchone() is not None
        assert conn.execute("select ts_lexize('intdict', '123')").fetchone() is not None


def test_index_access_methods(server: PGliteServer) -> None:
    with psycopg.connect(server.dsn, autocommit=True) as conn:
        conn.execute("create table ext_index_demo (a int, b text)")
        conn.execute(
            "create index ext_bloom_idx on ext_index_demo using bloom (a) "
            "with (length=32)"
        )
        conn.execute(
            "create index ext_gin_idx on ext_index_demo using gin (b gin_trgm_ops)"
        )
        conn.execute("create index ext_gist_idx on ext_index_demo using gist (a)")
        count = conn.execute(
            "select count(*) from pg_index i "
            "join pg_class c on c.oid = i.indexrelid "
            "where c.relname like 'ext_%_idx'"
        ).fetchone()
        assert count == (3,)
