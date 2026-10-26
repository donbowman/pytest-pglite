"""Unit tests for the compiled-module (AOT) cache."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from pytest_pglite import aot


class FakeWasmtimeError(Exception):
    pass


class FakeModule:
    """Stand-in for ``wasmtime.Module`` with the same serialise contract."""

    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def serialize(self) -> bytes:
        return self.payload

    @classmethod
    def from_file(cls, engine: object, path: str) -> FakeModule:
        return cls(Path(path).read_bytes())

    @classmethod
    def deserialize(cls, engine: object, data: bytes) -> FakeModule:
        if not data.startswith(b"module:"):
            raise FakeWasmtimeError(f"cannot deserialize {data!r}")
        return cls(data)


@pytest.fixture
def compiled(monkeypatch: pytest.MonkeyPatch) -> Iterator[type[FakeModule]]:
    monkeypatch.setattr(aot.wasmtime, "Module", FakeModule)
    monkeypatch.setattr(aot.wasmtime, "WasmtimeError", FakeWasmtimeError)
    yield FakeModule


def _wasm(tmp_path: Path, payload: bytes = b"module:compiled") -> Path:
    wasm = tmp_path / "pglite.wasi"
    wasm.write_bytes(payload)
    return wasm


def _temporaries(cache: Path) -> list[Path]:
    return [entry for entry in cache.iterdir() if entry.name.endswith(".tmp")]


def test_publishes_and_reuses_the_cached_module(
    tmp_path: Path, compiled: type[FakeModule]
) -> None:
    wasm = _wasm(tmp_path)
    cache = tmp_path / "aot"

    first = aot.load_module(object(), wasm, cache, "key")

    assert first.serialize() == b"module:compiled"
    assert (cache / "key.cwasm").read_bytes() == b"module:compiled"
    assert _temporaries(cache) == []

    # A hit deserialises instead of compiling: this wasm is now unusable.
    wasm.write_bytes(b"not a module")
    second = aot.load_module(object(), wasm, cache, "key")
    assert second.serialize() == b"module:compiled"


def test_corrupt_cache_entry_is_rebuilt(
    tmp_path: Path, compiled: type[FakeModule]
) -> None:
    wasm = _wasm(tmp_path, b"module:fresh")
    cache = tmp_path / "aot"
    cache.mkdir()
    (cache / "key.cwasm").write_bytes(b"garbage")

    module = aot.load_module(object(), wasm, cache, "key")

    assert module.serialize() == b"module:fresh"
    assert (cache / "key.cwasm").read_bytes() == b"module:fresh"
    assert _temporaries(cache) == []


def test_entry_that_vanishes_between_check_and_read_is_rebuilt(
    tmp_path: Path, compiled: type[FakeModule], monkeypatch: pytest.MonkeyPatch
) -> None:
    wasm = _wasm(tmp_path, b"module:fresh")
    cache = tmp_path / "aot"
    cache.mkdir()
    cached = cache / "key.cwasm"
    cached.write_bytes(b"module:cached")

    read_bytes = Path.read_bytes

    def flaky_read(self: Path) -> bytes:
        if self == cached:
            # Another worker unlinked it while repairing the same entry.
            raise FileNotFoundError(self)
        return read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", flaky_read)

    module = aot.load_module(object(), wasm, cache, "key")

    assert module.serialize() == b"module:fresh"
    assert read_bytes(cached) == b"module:fresh"


def test_failed_publish_never_breaks_the_run_and_leaves_no_temp(
    tmp_path: Path, compiled: type[FakeModule], monkeypatch: pytest.MonkeyPatch
) -> None:
    wasm = _wasm(tmp_path)
    cache = tmp_path / "aot"

    def readonly_replace(self: Path, target: Path) -> Path:
        raise OSError("read-only file system")

    monkeypatch.setattr(Path, "replace", readonly_replace)

    module = aot.load_module(object(), wasm, cache, "key")

    assert module.serialize() == b"module:compiled"
    assert not (cache / "key.cwasm").exists()
    assert _temporaries(cache) == []


def test_concurrent_writers_stage_under_distinct_names(
    tmp_path: Path, compiled: type[FakeModule], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two workers compiling one key must not share a staging file."""
    payloads = (b"module:one", b"module:two")
    wasm_paths = []
    for index, payload in enumerate(payloads):
        wasm = tmp_path / f"pglite-{index}.wasi"
        wasm.write_bytes(payload)
        wasm_paths.append(wasm)
    cache = tmp_path / "aot"

    # Hold both writers in serialize() until they are about to publish together.
    barrier = threading.Barrier(len(payloads))
    serialize = FakeModule.serialize

    def synchronised(self: FakeModule) -> bytes:
        barrier.wait(timeout=10)
        return serialize(self)

    monkeypatch.setattr(FakeModule, "serialize", synchronised)

    staged: list[Path] = []
    lock = threading.Lock()
    replace = Path.replace

    def recording(self: Path, target: Path) -> Path:
        with lock:
            staged.append(self)
        return replace(self, target)

    monkeypatch.setattr(Path, "replace", recording)

    errors: list[BaseException] = []

    def worker(path: Path) -> None:
        try:
            aot.load_module(object(), path, cache, "key")
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(path,)) for path in wasm_paths]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(20)

    assert errors == []
    assert len(staged) == len(payloads)
    assert len(set(staged)) == len(payloads)
    assert cache / "key.cwasm.tmp" not in staged
    assert (cache / "key.cwasm").read_bytes() in payloads
    assert _temporaries(cache) == []
