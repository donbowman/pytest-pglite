"""Unit tests for the prepared-data-directory cache."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from pytest_pglite.templates import (
    build_lock,
    copy_template,
    directory_size,
    prune_templates,
    publish_template,
    template_key,
)


def _write(path: Path, size: int, content: bytes = b"x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content * size)


def test_template_key_is_stable_and_change_sensitive() -> None:
    digest = "a" * 64
    first = template_key(digest, ("vector",), "alembic:0056")
    assert first == template_key(digest, ("vector",), "alembic:0056")
    assert first != template_key("b" * 64, ("vector",), "alembic:0056")
    assert first != template_key(digest, ("pg_trgm",), "alembic:0056")
    assert first != template_key(digest, ("vector",), "alembic:0057")
    assert template_key(digest, (), None) == f"{digest[:16]}-base"


def test_key_token_is_filesystem_safe() -> None:
    key = template_key("a" * 64, (), "../../etc/passwd:" + "x" * 100)
    assert "/" not in key
    assert ".." not in key
    assert len(key) < 120


def test_publish_replaces_atomically_and_cleans_staging(tmp_path: Path) -> None:
    base = tmp_path / "build" / "base"
    _write(base / "PG_VERSION", 1, b"17\n")
    template = tmp_path / "cache" / "key" / "base"

    publish_template(base, template)
    assert (template / "PG_VERSION").read_bytes() == b"17\n"

    _write(base / "PG_VERSION", 1, b"18\n")
    publish_template(base, template)
    assert (template / "PG_VERSION").read_bytes() == b"18\n"
    leftovers = [
        entry.name
        for entry in template.parent.iterdir()
        if entry.name.startswith((".staging-", ".trash-"))
    ]
    assert leftovers == []


def test_publish_replaces_a_corrupt_file(tmp_path: Path) -> None:
    base = tmp_path / "build" / "base"
    _write(base / "PG_VERSION", 1, b"17\n")
    template = tmp_path / "cache" / "key" / "base"
    template.parent.mkdir(parents=True)
    template.write_text("not a directory", encoding="utf-8")

    publish_template(base, template)
    assert template.is_dir()
    assert (template / "PG_VERSION").is_file()


def test_copy_template_replaces_the_destination(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _write(source / "PG_VERSION", 1, b"17\n")
    destination = tmp_path / "destination"
    _write(destination / "stale", 10)

    copy_template(source, destination)
    assert (destination / "PG_VERSION").read_bytes() == b"17\n"
    assert not (destination / "stale").exists()


def test_prune_removes_oldest_and_spares_the_current_key(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    keys = ["old", "middle", "new"]
    for index, key in enumerate(keys):
        _write(root / key / "base" / "PG_VERSION", 100, b"x")
        mtime = time.time() - (len(keys) - index) * 3600
        os.utime(root / key, (mtime, mtime))

    removed = prune_templates(
        root,
        keep_key="new",
        max_bytes=150,
        grace_seconds=0,
    )

    assert removed == ["old", "middle"]
    assert (root / "new" / "base" / "PG_VERSION").is_file()
    assert not (root / "old").exists()
    assert not (root / "middle").exists()


def test_prune_respects_the_grace_period(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    _write(root / "recent" / "base" / "PG_VERSION", 100)
    assert (
        prune_templates(root, keep_key="other", max_bytes=1, grace_seconds=3600) == []
    )


def test_prune_is_disabled_by_a_zero_cap(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    _write(root / "key" / "base" / "PG_VERSION", 100)
    assert prune_templates(root, keep_key="other", max_bytes=0) == []
    assert (root / "key" / "base").is_dir()


def test_directory_size(tmp_path: Path) -> None:
    _write(tmp_path / "a", 10)
    _write(tmp_path / "nested" / "b", 20)
    assert directory_size(tmp_path) == 30


def test_lock_times_out_without_waiting_forever(tmp_path: Path) -> None:
    started = time.monotonic()
    with build_lock(tmp_path, "key") as held:
        assert held
        with build_lock(tmp_path, "key", timeout=0.2) as second:
            assert second is False
    assert time.monotonic() - started >= 0.2


@pytest.mark.skipif(sys.platform == "win32", reason="requires flock")
def test_lock_is_released_when_the_builder_dies(tmp_path: Path) -> None:
    code = textwrap.dedent(f"""
        import time
        from pathlib import Path

        from pytest_pglite.templates import build_lock

        with build_lock(Path({str(tmp_path)!r}), "key") as acquired:
            assert acquired
            print("ready", flush=True)
            time.sleep(60)
        """)
    process = subprocess.Popen(
        [sys.executable, "-c", code],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "ready"
    finally:
        process.kill()
        process.wait(10)

    with build_lock(tmp_path, "key", timeout=5) as acquired:
        assert acquired
