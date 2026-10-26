"""Coarse performance guards.

These exist to catch catastrophic regressions, such as losing the AOT or
template caches. Caching is asserted on behaviour rather than wall clock: a
timing bound cannot reliably separate a cold start from a warm one, because a
cold start is only a couple of seconds on a fast machine while a warm start can
be slower still on a loaded CI runner.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from pytest_pglite import PGliteConfig, PGliteServer
from pytest_pglite import server as server_module

pytestmark = [pytest.mark.integration, pytest.mark.slow]


def test_query_round_trip_latency() -> None:
    with PGliteServer(PGliteConfig()) as server:
        import psycopg

        with psycopg.connect(server.dsn) as connection:
            # Warm up the connection, then time a batch of trivial queries.
            connection.execute("select 1")
            begin = time.perf_counter()
            for _ in range(100):
                connection.execute("select 1")
            per_query = (time.perf_counter() - begin) / 100
            connection.rollback()
    assert per_query < 0.05, f"query round trip took {per_query * 1000:.1f}ms"


def test_template_is_reused(monkeypatch: pytest.MonkeyPatch) -> None:
    """A second start copies the cached template instead of rebuilding it.

    A start that finds the template takes the copy fast path and never re-runs
    the snapshot; a start that rebuilds it is the regression worth catching.
    Both are directly observable, so there is no timing to be flaky.
    """
    snapshots: list[str] = []
    sources: list[str] = []
    original_snapshot = server_module.publish_template
    original_copy = server_module.copy_template

    def counting_snapshot(base: Path, template: Path) -> None:
        snapshots.append(str(template))
        original_snapshot(base, template)

    def counting_copy(source: Path, destination: Path) -> None:
        sources.append(str(source))
        original_copy(source, destination)

    monkeypatch.setattr(server_module, "publish_template", counting_snapshot)
    monkeypatch.setattr(server_module, "copy_template", counting_copy)

    with PGliteServer(PGliteConfig()) as server:
        server.execute("select 1")
        template = server._template_path()
    assert template is not None and template.is_dir(), "no template was cached"

    snapshots_after_first_start = len(snapshots)
    with PGliteServer(PGliteConfig()) as server:
        server.execute("select 1")

    rebuilt = len(snapshots) - snapshots_after_first_start
    assert rebuilt == 0, f"a second start rebuilt the template {rebuilt} time(s)"

    source = sources[-1] if sources else None
    assert source == str(template), f"a second start copied {source}"


def test_extended_protocol_pipeline_is_coalesced() -> None:
    """A parameterized query reaches the backend as one exchange, not five.

    Extended-protocol clients send Parse/Bind/Describe/Execute/Sync back to
    back; the proxy must feed them to the Wasmtime backend in one call or
    every query pays a Wasmtime round trip per message.
    """
    import psycopg

    with PGliteServer(PGliteConfig()) as server:
        engine = server._engine
        assert engine is not None
        calls: list[bytes] = []
        original = engine.exchange

        def counting_exchange(payload: bytes) -> bytes:
            calls.append(payload)
            return original(payload)

        engine.exchange = counting_exchange  # type: ignore[method-assign]
        # autocommit avoids psycopg's separate BEGIN/ROLLBACK simple queries.
        with psycopg.connect(server.dsn, autocommit=True) as connection:
            calls.clear()
            # Parameters force the extended query protocol.
            row = connection.execute("select %s::int", (1,)).fetchone()
            assert row == (1,)
        assert len(calls) == 1, f"expected one coalesced exchange, got {len(calls)}"
