"""The FastAPI app runs against the real PGlite database from the fixtures."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app import create_app


def test_users_roundtrip(pglite_url: str) -> None:
    app = create_app(pglite_url)
    with TestClient(app) as client:
        created = client.post("/users", json={"name": "Alice"})
        assert created.status_code == 201
        assert created.json() == {"id": 1, "name": "Alice"}

        client.post("/users", json={"name": "Bob"})
        listed = client.get("/users").json()
        assert listed == [
            {"id": 1, "name": "Alice"},
            {"id": 2, "name": "Bob"},
        ]


def test_pglite_execute_fixture(pglite_execute: object) -> None:
    assert pglite_execute("select 6 * 7 as answer") == [(42,)]
