"""On-disk cache for ahead-of-time compiled WASM modules.

Compiling the 23 MB pglite module takes about a second with Cranelift.
Wasmtime can serialise the compiled module and deserialise it in tens of
milliseconds, which makes per-worker startup much faster. Serialised modules
are only valid for the exact engine build that produced them, so the cache
key includes the wasmtime version and the platform.
"""

from __future__ import annotations

import hashlib
import platform
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import wasmtime

from .errors import PGliteArtifactError


def _wasmtime_version() -> str:
    try:
        return version("wasmtime")
    except PackageNotFoundError:  # pragma: no cover - vendored wasmtime
        return "unknown"


def cache_key(wasm_path: Path, artifact_digest: str | None = None) -> str:
    if artifact_digest is None:
        digest = hashlib.sha256(wasm_path.read_bytes()).hexdigest()
    else:
        digest = artifact_digest
    machine = platform.machine() or "unknown"
    return (
        f"wasmtime{_wasmtime_version()}-py{sys.version_info[0]}{sys.version_info[1]}-"
        f"{sys.platform}-{machine}-{digest[:16]}"
    )


def load_module(
    engine: wasmtime.Engine,
    wasm_path: Path,
    cache_dir: Path,
    key: str,
    use_cache: bool = True,
) -> wasmtime.Module:
    """Load a module, using the AOT cache when available."""
    cached = cache_dir / f"{key}.cwasm"
    if use_cache and cached.is_file():
        try:
            return wasmtime.Module.deserialize(engine, cached.read_bytes())
        except wasmtime.WasmtimeError:
            cached.unlink(missing_ok=True)
    try:
        module = wasmtime.Module.from_file(engine, str(wasm_path))
    except wasmtime.WasmtimeError as exc:  # pragma: no cover - corrupt artifact
        raise PGliteArtifactError(f"failed to compile {wasm_path}: {exc}") from exc
    if use_cache:
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
            temporary = cached.with_suffix(".cwasm.tmp")
            temporary.write_bytes(module.serialize())
            temporary.replace(cached)
        except OSError:
            # A read-only or full cache must never break the run.
            pass
    return module
