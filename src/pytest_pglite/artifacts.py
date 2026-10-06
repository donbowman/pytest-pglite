"""Locating, verifying and unpacking the bundled PGlite WASM artifact."""

from __future__ import annotations

import contextlib
import hashlib
import os
import shutil
import tarfile
from collections.abc import Iterator
from pathlib import Path

from platformdirs import user_cache_dir

from .errors import PGliteArtifactError

ARTIFACT_FILENAME = "pglite-17.5.20261006c.tar.xz"
WASM_DIR = Path(__file__).resolve().parent / "_wasm"
PREFIX_RELATIVE = Path("tmp") / "pglite"
WASM_RELATIVE = Path("bin") / "pglite.wasi"


def artifact_path() -> Path:
    """Return the path of the main vendored artifact tarball."""
    return WASM_DIR / ARTIFACT_FILENAME


def artifact_paths() -> tuple[Path, ...]:
    """All vendored tarballs, in extraction order."""
    return (artifact_path(),)


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def expected_digest(filename: str = ARTIFACT_FILENAME) -> str:
    sums = WASM_DIR / "SHA256SUMS"
    if not sums.is_file():
        raise PGliteArtifactError(f"missing checksum file: {sums}")
    for line in sums.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] == filename:
            return parts[0]
    raise PGliteArtifactError(f"no checksum recorded for {filename}")


def verify_artifact(path: Path | None = None) -> str:
    """Verify a vendored tarball and return its sha256 digest."""
    path = path or artifact_path()
    if not path.is_file():
        raise PGliteArtifactError(
            f"bundled PGlite artifact not found at {path}; "
            "install the package from a wheel/sdist that includes it"
        )
    digest = sha256_file(path)
    expected = expected_digest(path.name)
    if digest != expected:
        raise PGliteArtifactError(
            f"PGlite artifact checksum mismatch for {path}: "
            f"expected {expected}, got {digest}"
        )
    return digest


def _extract_tar(path: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(path, "r:xz") as archive:
        for member in archive.getmembers():
            member_path = Path(member.name)
            if member_path.is_absolute() or ".." in member_path.parts:
                raise PGliteArtifactError(
                    f"refusing to extract unsafe path {member.name!r} from {path}"
                )
        archive.extractall(destination, filter="data")


@contextlib.contextmanager
def _cache_lock(lock_path: Path) -> Iterator[None]:
    """Serialise cold-start extraction of one artifact across processes."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w", encoding="utf-8") as handle:
        try:
            import fcntl
        except ImportError:  # pragma: no cover - non-POSIX platforms
            pass
        else:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield


def ensure_prefix(
    wasm_path: Path | None = None, use_cache: bool = True
) -> tuple[Path, str]:
    """Return (prefix_root, cache_key).

    ``prefix_root`` contains ``bin/pglite.wasi`` plus the ``share`` and ``lib``
    trees the WASM module needs. For the bundled artifact the tarball is
    verified and extracted once into the user cache directory.

    Cold-start extraction is protected by a per-artifact file lock and the
    result is published with an atomic rename, so concurrent workers (for
    example pytest-xdist processes) never observe a half-written prefix.
    """
    if wasm_path is not None:
        candidate = wasm_path.expanduser()
        if candidate.is_file():
            candidate = candidate.parent.parent
        if not (candidate / WASM_RELATIVE).is_file():
            raise PGliteArtifactError(
                f"custom wasm path {wasm_path} does not contain bin/pglite.wasi"
            )
        digest = sha256_file(candidate / WASM_RELATIVE)
        return candidate, digest

    path = artifact_path()
    digest = verify_artifact(path)
    cache_parent = Path(user_cache_dir("pytest-pglite")) / "artifacts"
    cache_root = cache_parent / digest[:16]
    marker = cache_root / ".complete"
    prefix = cache_root / PREFIX_RELATIVE

    def cached() -> bool:
        return use_cache and marker.is_file() and (prefix / WASM_RELATIVE).is_file()

    if cached():
        return prefix, digest

    cache_parent.mkdir(parents=True, exist_ok=True)
    with _cache_lock(cache_parent / f"{cache_root.name}.lock"):
        # Another worker may have finished while we waited for the lock.
        if cached():
            return prefix, digest

        # Extract into a private staging directory and publish it with an
        # atomic rename; readers only ever see the finished tree.  Any other
        # staging directory for this artifact is a leftover from an aborted
        # extraction and is safe to remove under the lock.
        for stale in cache_parent.glob(f".staging-{cache_root.name}-*"):
            shutil.rmtree(stale, ignore_errors=True)
        staging = cache_parent / f".staging-{cache_root.name}-{os.getpid()}"
        try:
            for tarball in artifact_paths():
                if tarball.is_file():
                    verify_artifact(tarball)
                    _extract_tar(tarball, staging)
            extracted = staging / PREFIX_RELATIVE / WASM_RELATIVE
            if not extracted.is_file():
                raise PGliteArtifactError(
                    f"extracted artifact is missing {PREFIX_RELATIVE / WASM_RELATIVE}"
                )
            (staging / ".complete").write_text(digest, encoding="utf-8")
            if cache_root.exists():
                shutil.rmtree(cache_root)
            os.replace(staging, cache_root)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
    return prefix, digest


def _link_or_copy(source: str, destination: str) -> str:
    try:
        os.link(source, destination)
        return destination
    except OSError:
        return shutil.copy2(source, destination)


def prepare_work_dir(prefix: Path, work_dir: Path) -> Path:
    """Copy the prefix into a fresh work directory and return the work dir.

    Hard links are used when possible so that starting a worker is cheap; the
    WASM file is large but read-only.
    """
    target = work_dir / PREFIX_RELATIVE
    if target.exists():
        raise PGliteArtifactError(f"work directory already populated: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(prefix, target, copy_function=_link_or_copy, symlinks=False)
    dev = work_dir / "dev"
    dev.mkdir(parents=True, exist_ok=True)
    urandom = dev / "urandom"
    if not urandom.exists():
        urandom.write_bytes(os.urandom(256))
    return work_dir


def wasm_file(prefix: Path) -> Path:
    wasm = prefix / WASM_RELATIVE
    if not wasm.is_file():
        raise PGliteArtifactError(f"missing WASM module: {wasm}")
    return wasm
