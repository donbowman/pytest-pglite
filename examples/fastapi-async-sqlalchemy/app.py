"""FastAPI + SQLAlchemy asyncio on PGlite."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine


class NoteIn(BaseModel):
    title: str


def create_app(database_url: str) -> FastAPI:
    engine: AsyncEngine = create_async_engine(database_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "create table if not exists notes ("
                    "id serial primary key, title text not null)"
                )
            )
        yield
        await engine.dispose()

    app = FastAPI(lifespan=lifespan)

    @app.post("/notes", status_code=201)
    async def create_note(note: NoteIn) -> dict[str, object]:
        async with session_factory() as session:
            row = (
                await session.execute(
                    text(
                        "insert into notes (title) values (:title) returning id, title"
                    ),
                    {"title": note.title},
                )
            ).one()
            await session.commit()
            return {"id": row.id, "title": row.title}

    @app.get("/notes")
    async def list_notes() -> list[dict[str, object]]:
        async with session_factory() as session:
            rows = (await session.execute(text("select id, title from notes"))).all()
            return [{"id": row.id, "title": row.title} for row in rows]

    return app
