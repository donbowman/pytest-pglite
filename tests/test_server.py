"""Integration tests for the PGlite server and wire protocol."""

from __future__ import annotations

import threading
from collections.abc import Iterator

import pytest

from pytest_pglite import PGliteConfig, PGliteServer

psycopg = pytest.importorskip("psycopg")

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def server() -> Iterator[PGliteServer]:
    with PGliteServer(PGliteConfig(tcp=True)) as running:
        yield running


def test_version_and_dsn(server: PGliteServer) -> None:
    assert "host=" in server.dsn
    assert server.port
    with psycopg.connect(server.dsn) as conn:
        (version,) = conn.execute("select version()").fetchone()
        assert "PostgreSQL 17.5" in version


def test_crud_and_types(server: PGliteServer) -> None:
    with psycopg.connect(server.dsn) as conn:
        conn.execute(
            "create table items (id serial primary key, data jsonb, tags text[])"
        )
        conn.execute(
            "insert into items (data, tags) values (%s, %s)",
            ('{"a": 1}', ["x", "y"]),
        )
        row = conn.execute(
            "select id, data->>'a' as a, array_length(tags, 1) as n from items"
        ).fetchone()
        assert row == (1, "1", 2)
        conn.rollback()


def test_error_reports_sqlstate_and_recovers(server: PGliteServer) -> None:
    with psycopg.connect(server.dsn) as conn:
        with pytest.raises(psycopg.errors.UndefinedTable) as excinfo:
            conn.execute("select * from missing_table")
        assert excinfo.value.sqlstate == "42P01"
        conn.rollback()
        assert conn.execute("select 1 as ok").fetchone() == (1,)


def test_unique_violation_sqlstate(server: PGliteServer) -> None:
    with psycopg.connect(server.dsn) as conn:
        conn.execute("create table u (id int primary key)")
        conn.execute("insert into u values (1)")
        with pytest.raises(psycopg.errors.UniqueViolation) as excinfo:
            conn.execute("insert into u values (1)")
        assert excinfo.value.sqlstate == "23505"
        conn.rollback()


def test_transactions_and_savepoints(server: PGliteServer) -> None:
    with psycopg.connect(server.dsn) as conn:
        conn.execute("create table tx (id int)")
        conn.commit()
        with conn.transaction():
            conn.execute("insert into tx values (1)")
        assert conn.execute("select count(*) from tx").fetchone() == (1,)
        try:
            with conn.transaction():
                conn.execute("insert into tx values (2)")
                raise RuntimeError("force rollback")
        except RuntimeError:
            pass
        assert conn.execute("select count(*) from tx").fetchone() == (1,)
        conn.execute("savepoint sp")
        conn.execute("insert into tx values (3)")
        conn.execute("rollback to savepoint sp")
        conn.commit()
        assert conn.execute("select count(*) from tx").fetchone() == (1,)


def test_prepared_statements(server: PGliteServer) -> None:
    with psycopg.connect(server.dsn) as conn:
        for index in range(8):
            assert conn.execute("select %s::int * 2 as n", (index,)).fetchone() == (
                index * 2,
            )
        conn.rollback()


def test_concurrent_connections(server: PGliteServer) -> None:
    errors: list[str] = []

    def worker(name: str) -> None:
        try:
            with psycopg.connect(server.dsn) as conn:
                for index in range(5):
                    with conn.transaction():
                        conn.execute(
                            "insert into conc values (%s)", (f"{name}-{index}",)
                        )
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{name}: {exc!r}")

    with psycopg.connect(server.dsn) as conn:
        conn.execute("create table conc (v text)")
        conn.commit()
    threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    with psycopg.connect(server.dsn) as conn:
        assert conn.execute("select count(*) from conc").fetchone() == (15,)
        conn.rollback()


def test_server_execute_helper(server: PGliteServer) -> None:
    rows = server.execute("select 1 as one, 'two' as two")
    assert rows == [["1", "two"]]


async def test_asyncpg_roundtrip(server: PGliteServer) -> None:
    asyncpg = pytest.importorskip("asyncpg")
    connection = await asyncpg.connect(
        host=server.config.tcp_host,
        port=server.port,
        user="postgres",
        password="password",
        database="template1",
        server_settings={},
    )
    try:
        assert await connection.fetchval("select $1::int + $2::int", 20, 22) == 42
        with pytest.raises(asyncpg.exceptions.UndefinedTableError):
            await connection.execute("select * from missing_table")
        assert await connection.fetchval("select 5") == 5
    finally:
        await connection.close()
