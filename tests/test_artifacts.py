"""Tests for artifact verification and work-directory preparation."""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from pytest_pglite import artifacts

_COLD_START_CHILD = """
import hashlib
import os
import sys
import time
from pathlib import Path

import pytest_pglite.artifacts as artifacts

ready_dir, cache, counter = sys.argv[1], sys.argv[2], sys.argv[3]
artifacts.user_cache_dir = lambda app=None: cache

original = artifacts._extract_tar


def counting_extract(path, destination):
    original(path, destination)
    with open(counter, "a", encoding="utf-8") as handle:
        handle.write("x")
    time.sleep(1.0)


artifacts._extract_tar = counting_extract

# Line every worker up before any of them enters ensure_prefix(), so the
# cold-start window is exercised deterministically.
ready = Path(ready_dir)
(ready / str(os.getpid())).touch()
while not (ready / "go").exists():
    time.sleep(0.01)

prefix, digest = artifacts.ensure_prefix()
data = (prefix / "bin" / "pglite.wasi").read_bytes()
print(hashlib.sha256(data).hexdigest(), len(data))
"""


def test_artifact_checksum() -> None:
    digest = artifacts.verify_artifact()
    assert digest == artifacts.expected_digest()


def test_ensure_prefix_extracts_and_caches(tmp_path: pytest.TempPathFactory) -> None:
    prefix, digest = artifacts.ensure_prefix()
    assert (prefix / "bin" / "pglite.wasi").is_file()
    assert (prefix / "password").is_file()
    assert len(digest) == 64
    second, digest2 = artifacts.ensure_prefix()
    assert second == prefix
    assert digest2 == digest


def test_prepare_work_dir(tmp_path: object) -> None:
    from pathlib import Path

    work = Path(str(tmp_path)) / "work"
    work.mkdir()
    prefix, _ = artifacts.ensure_prefix()
    artifacts.prepare_work_dir(prefix, work)
    assert (work / "tmp" / "pglite" / "bin" / "pglite.wasi").is_file()
    assert (work / "dev" / "urandom").is_file()


def test_missing_artifact_raises(tmp_path: object) -> None:
    from pathlib import Path

    from pytest_pglite.errors import PGliteArtifactError

    with pytest.raises(PGliteArtifactError):
        artifacts.verify_artifact(Path(str(tmp_path)) / "nope.tar.xz")


def test_ensure_prefix_cold_start_extracts_once(tmp_path: Path) -> None:
    """Concurrent cold starts serialise on the lock and extract once.

    Regression test: every worker used to see the missing marker, remove the
    cache root and extract into it concurrently, so a worker could compile a
    partially written pglite.wasi (CI failures showed parse errors at
    different offsets in different workers).
    """
    ready_dir = tmp_path / "ready"
    ready_dir.mkdir()
    cache = tmp_path / "cache"
    counter = tmp_path / "extractions"
    processes = [
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                _COLD_START_CHILD,
                str(ready_dir),
                str(cache),
                str(counter),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(4)
    ]
    try:
        deadline = time.monotonic() + 60
        while len(list(ready_dir.iterdir())) < len(processes):
            assert time.monotonic() < deadline, "workers did not reach the barrier"
            time.sleep(0.01)
        (ready_dir / "go").touch()

        results = set()
        for process in processes:
            out, err = process.communicate(timeout=300)
            assert process.returncode == 0, err
            results.add(out.strip())
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()

    assert len(results) == 1
    assert counter.read_text(encoding="utf-8") == "x"
    digest = artifacts.expected_digest()
    assert (cache / "artifacts" / digest[:16] / ".complete").is_file()


def test_ensure_prefix_replaces_partial_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = tmp_path / "cache"
    monkeypatch.setattr(artifacts, "user_cache_dir", lambda app=None: str(cache))
    digest = artifacts.expected_digest()
    cache_root = cache / "artifacts" / digest[:16]
    partial = cache_root / "tmp" / "pglite" / "bin"
    partial.mkdir(parents=True)
    (partial / "pglite.wasi").write_bytes(b"partial")
    stale = cache_root.parent / f".staging-{digest[:16]}-9999"
    stale.mkdir(parents=True)

    prefix, _ = artifacts.ensure_prefix()

    assert (prefix / "bin" / "pglite.wasi").stat().st_size > 1_000_000
    assert (cache_root / ".complete").is_file()
    assert not stale.exists()
