"""A tiny suite intended to be run with pytest-xdist.

Each xdist worker starts its own PGlite instance with its own temporary
directory, socket and (when enabled) OS-assigned TCP port. Run this file with
``pytest -n 4 --pglite-isolation=none`` to exercise worker isolation: with
``isolation=none`` state persists within a worker, so a table created by one
worker must never be visible to another.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.integration


def _worker_id() -> str:
    return os.environ.get("PYTEST_XDIST_WORKER", "master")


def _ensure_marker(execute: object) -> str:
    table = f"wx_{_worker_id()}"
    execute(f"create table if not exists {table} (worker text)")  # noqa: S608
    execute(f"truncate {table}")  # noqa: S608
    execute(f"insert into {table} values (%s)", (_worker_id(),))  # noqa: S608
    return table


def test_worker_sees_its_own_data(pglite_execute: object) -> None:
    table = _ensure_marker(pglite_execute)
    assert pglite_execute(f"select worker from {table}") == [
        (_worker_id(),)
    ]  # noqa: S608


def test_workers_do_not_share_tables(pglite_execute: object) -> None:
    _ensure_marker(pglite_execute)
    rows = pglite_execute("select tablename from pg_tables where tablename like 'wx_%'")
    assert rows == [(f"wx_{_worker_id()}",)]
