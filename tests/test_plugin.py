"""Integration tests for the pytest fixtures themselves."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


def test_pglite_dsn(pglite_dsn: str) -> None:
    assert "postgresql://" in pglite_dsn


def test_pglite_conn_and_isolation(pglite_conn: object, pglite_execute: object) -> None:
    assert pglite_execute("select 1 as n") == [(1,)]
    pglite_execute("create table isolated (id int)")
    assert pglite_execute("select count(*) from isolated") == [(0,)]


def test_pglite_conn_schema_is_fresh(pglite_execute: object) -> None:
    # The previous test created "isolated"; schema isolation must have dropped it.
    with pytest.raises(Exception):
        pglite_execute("select count(*) from isolated")


def test_pglite_engine(pglite_engine: object) -> None:
    from sqlalchemy import text

    with pglite_engine.begin() as connection:
        connection.execute(
            text("create table orm_items (id serial primary key, name text)")
        )
        connection.execute(text("insert into orm_items (name) values ('alice')"))
        count = connection.execute(text("select count(*) from orm_items")).scalar_one()
        assert count == 1


async def test_pglite_async_conn(pglite_async_conn: object) -> None:
    cursor = pglite_async_conn.cursor()
    await cursor.execute("select 2 + 2 as n")
    assert await cursor.fetchone() == (4,)
    await pglite_async_conn.rollback()
    await cursor.close()


async def test_pglite_asyncpg(pglite_asyncpg: object) -> None:
    assert await pglite_asyncpg.fetchval("select 6 * 7") == 42
