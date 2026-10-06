"""A tiny FastAPI application backed by a synchronous SQLAlchemy engine."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from pydantic import BaseModel
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker


class UserIn(BaseModel):
    name: str


def create_app(database_url: str) -> FastAPI:
    engine: Engine = create_engine(database_url)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "create table if not exists users ("
                    "id serial primary key, name text not null)"
                )
            )
        yield
        engine.dispose()

    app = FastAPI(lifespan=lifespan)

    @app.post("/users", status_code=201)
    def create_user(user: UserIn) -> dict[str, object]:
        with session_factory() as session:
            row = session.execute(
                text("insert into users (name) values (:name) returning id, name"),
                {"name": user.name},
            ).one()
            session.commit()
            return {"id": row.id, "name": row.name}

    @app.get("/users")
    def list_users() -> list[dict[str, object]]:
        with session_factory() as session:
            rows: list[Session] = session.execute(
                text("select id, name from users order by id")
            ).all()
            return [{"id": row.id, "name": row.name} for row in rows]

    return app
