"""asyncpg against PGlite, using the plugin's asyncpg fixture."""

from __future__ import annotations


async def test_crud(pglite_asyncpg: object) -> None:
    await pglite_asyncpg.execute(
        "create table things (id serial primary key, name text not null)"
    )
    await pglite_asyncpg.execute("insert into things (name) values ($1)", "first")
    assert await pglite_asyncpg.fetchval("select count(*) from things") == 1
    row = await pglite_asyncpg.fetchrow("select id, name from things")
    assert row["name"] == "first"


async def test_prepared_statements(pglite_asyncpg: object) -> None:
    for value in range(6):
        assert await pglite_asyncpg.fetchval("select $1::int + 1", value) == value + 1


async def test_error_recovers(pglite_asyncpg: object) -> None:
    import pytest

    with pytest.raises(Exception):
        await pglite_asyncpg.execute("select * from does_not_exist")
    assert await pglite_asyncpg.fetchval("select 7") == 7
