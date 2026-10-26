"""Integration tests for the prepare hook and the prepared-template cache."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import textwrap
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from pytest_pglite import IsolationMode, PGliteConfig, PGliteServer

psycopg = pytest.importorskip("psycopg")

pytestmark = pytest.mark.integration


@pytest.fixture
def template_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point the server at a per-test template cache, whichever OS is running."""
    root = tmp_path / "templates"
    monkeypatch.setattr("pytest_pglite.server.cache_root", lambda: root)
    yield root


def _config(prepare: Any, key: str, **overrides: Any) -> PGliteConfig:
    return PGliteConfig(
        prepare=prepare,
        prepare_key=key,
        isolation=IsolationMode.NONE,
        **overrides,
    )


@pytest.mark.slow
def test_prepare_runs_once_and_is_reused(tmp_path: Path, template_cache: Path) -> None:
    calls: list[int] = []

    def migrate(server: PGliteServer) -> None:
        calls.append(1)
        with psycopg.connect(server.dsn) as conn:
            conn.execute("create table seeded (id int)")
            conn.commit()

    with PGliteServer(_config(migrate, "schema-1")) as first:
        assert first.execute("select count(*) from seeded") == [["0"]]
    assert calls == [1]
    assert list(template_cache.glob("*/base"))

    # A second session with the same key copies the prepared directory and
    # never runs the migration.
    with PGliteServer(_config(migrate, "schema-1")) as second:
        assert second.execute("select count(*) from seeded") == [["0"]]
    assert calls == [1]

    # A new revision invalidates the cache.
    with PGliteServer(_config(migrate, "schema-2")) as third:
        assert third.execute("select count(*) from seeded") == [["0"]]
    assert calls == [1, 1]

    # The forced-rebuild switch runs prepare even though the key exists.
    with PGliteServer(_config(migrate, "schema-2", rebuild_templates=True)) as fourth:
        assert fourth.execute("select count(*) from seeded") == [["0"]]
    assert calls == [1, 1, 1]


@pytest.mark.slow
def test_corrupt_template_falls_back_to_a_build(
    tmp_path: Path, template_cache: Path
) -> None:
    calls: list[int] = []

    def migrate(server: PGliteServer) -> None:
        calls.append(1)
        with psycopg.connect(server.dsn) as conn:
            conn.execute("create table seeded (id int)")
            conn.commit()

    with PGliteServer(_config(migrate, "corrupt")) as first:
        assert first.execute("select count(*) from seeded") == [["0"]]
    assert calls == [1]

    base = next(template_cache.glob("*/base"))
    # Make the checkpoint unusable: the guest cannot start without pg_control.
    shutil.rmtree(base / "global")

    with PGliteServer(_config(migrate, "corrupt")) as second:
        assert second.execute("select count(*) from seeded") == [["0"]]
    assert calls == [1, 1]


@pytest.mark.slow
def test_prepare_runs_once_across_xdist_workers(tmp_path: Path) -> None:
    pytest.importorskip("xdist")
    calls = tmp_path / "calls"
    cache = tmp_path / "templates"
    conftest = tmp_path / "conftest.py"
    conftest.write_text(
        textwrap.dedent(f"""
            import pathlib

            import psycopg

            import pytest_pglite.server as server_module
            from pytest_pglite import IsolationMode

            CALLS = pathlib.Path({str(calls)!r})
            server_module.cache_root = lambda: pathlib.Path({str(cache)!r})


            def migrate(server):
                with CALLS.open("a") as handle:
                    handle.write("x")
                with psycopg.connect(server.dsn) as conn:
                    conn.execute("create table seeded (id int)")
                    conn.commit()


            def pytest_pglite_configure(pglite_config):
                pglite_config.prepare = migrate
                pglite_config.prepare_key = "xdist-1"
                pglite_config.isolation = IsolationMode.NONE
                pglite_config.startup_timeout = 180
            """),
        encoding="utf-8",
    )
    (tmp_path / "test_seeded.py").write_text(
        "def test_seeded(pglite_execute):\n"
        "    assert pglite_execute('select count(*) from seeded') == [(0,)]\n",
        encoding="utf-8",
    )

    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        filter(
            None,
            [str(Path(__file__).resolve().parents[1] / "src"), env.get("PYTHONPATH")],
        )
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-n",
            "2",
            "-q",
            "-p",
            "no:cacheprovider",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert calls.read_text(encoding="utf-8") == "x"
