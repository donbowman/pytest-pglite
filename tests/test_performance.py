"""Coarse performance guards.

These use generous bounds so they only fail on catastrophic regressions
(for example, losing the AOT or template caches).
"""

from __future__ import annotations

import time

import pytest

from pytest_pglite import PGliteConfig, PGliteServer

pytestmark = [pytest.mark.integration, pytest.mark.slow]


def test_warm_start_and_query_latency() -> None:
    started = time.perf_counter()
    with PGliteServer(PGliteConfig()) as server:
        startup = time.perf_counter() - started
        import psycopg

        with psycopg.connect(server.dsn) as connection:
            # Warm up the connection, then time a batch of trivial queries.
            connection.execute("select 1")
            begin = time.perf_counter()
            for _ in range(100):
                connection.execute("select 1")
            per_query = (time.perf_counter() - begin) / 100
            connection.rollback()
    assert startup < 10.0, f"warm start took {startup:.2f}s"
    assert per_query < 0.05, f"query round trip took {per_query * 1000:.1f}ms"


def test_template_cache_reuse() -> None:
    first = time.perf_counter()
    with PGliteServer(PGliteConfig()) as server:
        server.execute("select 1")
    first_elapsed = time.perf_counter() - first
    second = time.perf_counter()
    with PGliteServer(PGliteConfig()) as server:
        server.execute("select 1")
    second_elapsed = time.perf_counter() - second
    # Both should be fast; the second must not be an order of magnitude worse.
    assert second_elapsed < max(
        5.0, first_elapsed * 3
    ), f"second start {second_elapsed:.2f}s vs first {first_elapsed:.2f}s"
