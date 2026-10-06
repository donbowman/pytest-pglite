"""SQL errors recover inside the WASM instance instead of trapping it.

The bundled module enables PostgreSQL's sigsetjmp/longjmp error handling, so
an ERROR unwinds to the normal recovery point and the backend keeps running.
A trap count above zero means the instance aborted and the host recovery path
ran instead.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from pytest_pglite import PGliteConfig, PGliteServer

psycopg = pytest.importorskip("psycopg")

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def server() -> Iterator[PGliteServer]:
    with PGliteServer(PGliteConfig()) as running:
        yield running


def test_simple_query_errors_do_not_trap(server: PGliteServer) -> None:
    with psycopg.connect(server.dsn) as conn:
        for _ in range(3):
            with pytest.raises(psycopg.errors.UndefinedTable):
                conn.execute("select * from table_that_does_not_exist")
            conn.rollback()
        assert conn.execute("select 1").fetchone() == (1,)
        conn.rollback()
    assert server.trap_count == 0


def test_error_inside_transaction(server: PGliteServer) -> None:
    with psycopg.connect(server.dsn) as conn:
        conn.execute("create temp table keep_me (n int)")
        conn.commit()
        with pytest.raises(psycopg.errors.DivisionByZero):
            conn.execute("select 1 / 0")
        conn.rollback()
        # The aborted transaction is rolled back but the session survives.
        assert conn.execute("select count(*) from keep_me").fetchone() == (0,)
        conn.commit()
    assert server.trap_count == 0


def test_error_then_continue_on_another_connection(server: PGliteServer) -> None:
    with psycopg.connect(server.dsn) as first:
        with pytest.raises(psycopg.errors.UndefinedColumn):
            first.execute("select 1 as one, column_that_does_not_exist")
        first.rollback()
        with psycopg.connect(server.dsn) as second:
            assert second.execute("select 2").fetchone() == (2,)
            second.rollback()
    assert server.trap_count == 0
