"""Tests for artifact verification and work-directory preparation."""

from __future__ import annotations

import pytest

from pytest_pglite import artifacts


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
