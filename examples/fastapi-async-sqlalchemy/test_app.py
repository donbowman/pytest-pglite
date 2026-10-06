"""Async FastAPI tests using SQLAlchemy asyncio and httpx.

``httpx.ASGITransport`` does not run ASGI lifespan events, so the schema is
created explicitly through the plugin's engine fixture. A real server started
with uvicorn runs the app's lifespan and creates the table itself.
"""

from __future__ import annotations

import httpx

from app import create_app


async def test_notes_roundtrip(pglite_url: str, pglite_engine: object) -> None:
    with pglite_engine.begin() as connection:
        connection.exec_driver_sql(
            "create table if not exists notes ("
            "id serial primary key, title text not null)"
        )
    app = create_app(pglite_url)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post("/notes", json={"title": "first"})
        assert created.status_code == 201
        listed = await client.get("/notes")
        assert listed.json() == [{"id": 1, "title": "first"}]


async def test_async_connection_fixture(pglite_async_conn: object) -> None:
    cursor = pglite_async_conn.cursor()
    await cursor.execute("select array[1, 2, 3] as nums")
    assert await cursor.fetchone() == ([1, 2, 3],)
    await pglite_async_conn.rollback()
    await cursor.close()
