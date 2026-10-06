"""A psql smoke test, skipped when the client is not installed.

CI installs postgresql-client on Linux, so the real libpq client is exercised
against the proxy at least once per build.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Iterator

import pytest

from pytest_pglite import PGliteConfig, PGliteServer

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def server() -> Iterator[PGliteServer]:
    with PGliteServer(PGliteConfig()) as running:
        yield running


def test_psql_select(server: PGliteServer) -> None:
    psql = shutil.which("psql")
    if psql is None:
        pytest.skip("psql is not installed")
    env = dict(os.environ, PGSSLMODE="disable", PGPASSWORD="password")
    result = subprocess.run(
        [psql, server.dsn, "-Atc", "select 1 + 1"],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "2"


def test_psql_meta_commands(server: PGliteServer) -> None:
    psql = shutil.which("psql")
    if psql is None:
        pytest.skip("psql is not installed")
    env = dict(os.environ, PGSSLMODE="disable", PGPASSWORD="password")
    script = (
        "create table psql_demo (id int);\n"
        "insert into psql_demo values (1);\n"
        "select count(*) from psql_demo;\n"
    )
    result = subprocess.run(
        [psql, server.dsn, "-At"],
        input=script,
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "1" in result.stdout
