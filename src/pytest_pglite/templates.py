"""On-disk cache of prepared PGlite data directories.

A template is the ``base`` directory of an initialised PostgreSQL cluster, at
``<cache root>/<key>/base``. Workers copy it into their own work directory
before starting the guest, so a whole xdist fleet starts from one build.

Keys combine the artifact digest, the extension list and a caller-supplied
schema revision, so a PostgreSQL bump or a schema change misses the cache
instead of resuming a database with the wrong catalog. Publishing is
serialised by a ``flock`` that the kernel drops if the builder dies, and the
finished copy is renamed into place, so a reader never sees a partial tree.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import re
import shutil
import time
from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from pathlib import Path

from platformdirs import user_cache_dir

#: Seconds before an unused template may be pruned.
PRUNE_GRACE_SECONDS = 60.0

#: How often a waiting builder retries a non-blocking ``flock``.
_LOCK_POLL_SECONDS = 0.1


def cache_root() -> Path:
    """The directory holding every template, regardless of key."""
    return Path(user_cache_dir("pytest-pglite")) / "templates"


def _key_token(value: str) -> str:
    """Turn a caller's revision string into a readable, bounded key part."""
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip(".-")[:40]
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:10]
    return f"{slug}-{digest}" if slug else digest


def callable_identity(prepare: Callable[..., object]) -> str:
    """A stable-ish name for a prepare callable, used when no key is given."""
    module = getattr(prepare, "__module__", "") or ""
    name = getattr(prepare, "__qualname__", "") or type(prepare).__qualname__
    return f"{module}.{name}" if module else name


def template_key(
    artifact_digest: str,
    extensions: Sequence[str],
    prepare_key: str | None = None,
) -> str:
    """Build the cache key for one artifact, extension set and schema revision."""
    extension_part = "_".join(extensions) or "base"
    key = f"{artifact_digest[:16]}-{extension_part}"
    if prepare_key:
        key = f"{key}-{_key_token(prepare_key)}"
    return key


def template_base(root: Path, key: str) -> Path:
    """The ``base`` directory of the template stored under ``key``."""
    return root / key / "base"


@contextlib.contextmanager
def build_lock(root: Path, key: str, *, timeout: float | None = None) -> Iterator[bool]:
    """Hold an exclusive build lock for ``key``.

    Yields True when the lock was acquired. ``flock`` is released by the
    kernel if the process dies, so a crashed builder cannot wedge the run.
    When ``timeout`` expires the caller proceeds without the lock (the build
    still publishes atomically); the returned flag lets it explain that in the
    log. Platforms without ``fcntl`` get no cross-process locking, matching
    the rest of the artifact cache.
    """
    try:
        root.mkdir(parents=True, exist_ok=True)
        lock_path = root / f"{key}.lock"
        handle = lock_path.open("a+", encoding="utf-8")
    except OSError:
        # An unwritable cache directory must not stop the session; the build
        # proceeds and publishing fails separately (also a warning).
        yield False
        return
    acquired = False
    try:
        try:
            import fcntl
        except ImportError:  # pragma: no cover - non-POSIX platforms
            acquired = True
        else:
            if timeout is None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                acquired = True
            else:
                deadline = time.monotonic() + timeout
                while True:
                    try:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            break
                        time.sleep(_LOCK_POLL_SECONDS)
                    else:
                        acquired = True
                        break
        yield acquired
    finally:
        if acquired:
            with contextlib.suppress(OSError):
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


@contextlib.asynccontextmanager
async def async_build_lock(
    root: Path, key: str, *, timeout: float | None = None
) -> AsyncIterator[bool]:
    """Async variant of :func:`build_lock` for the server's event loop.

    Waiting is a non-blocking ``flock`` retry with ``await asyncio.sleep``, so
    the loop still serves shutdown requests while a worker waits for another
    builder.
    """
    try:
        root.mkdir(parents=True, exist_ok=True)
        lock_path = root / f"{key}.lock"
        handle = lock_path.open("a+", encoding="utf-8")
    except OSError:
        # An unwritable cache directory must not stop the session; the build
        # proceeds and publishing fails separately (also a warning).
        yield False
        return
    acquired = False
    try:
        try:
            import fcntl
        except ImportError:  # pragma: no cover - non-POSIX platforms
            acquired = True
        else:
            loop = asyncio.get_running_loop()
            deadline = None if timeout is None else loop.time() + timeout
            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    if deadline is not None and loop.time() >= deadline:
                        break
                    await asyncio.sleep(_LOCK_POLL_SECONDS)
                else:
                    acquired = True
                    break
        yield acquired
    finally:
        if acquired:
            with contextlib.suppress(OSError):
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def copy_template(source: Path, destination: Path) -> None:
    """Replace ``destination`` with a copy of ``source``.

    Any existing destination is removed first: a resumed guest must start from
    the template's checkpoint, not from a half-copied or stale tree.
    """
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(source, destination, copy_function=shutil.copy2)


def publish_template(base: Path, template: Path) -> None:
    """Atomically publish a checkpointed ``base`` as ``template``.

    The copy happens in a staging directory on the same filesystem and is
    renamed into place, so another process either sees the previous template
    or the new one, never a partial tree.
    """
    root = template.parent
    root.mkdir(parents=True, exist_ok=True)
    _remove_stale(root, template.name)
    staging = root / f".staging-{template.name}-{os.getpid()}"
    shutil.rmtree(staging, ignore_errors=True)
    try:
        shutil.copytree(base, staging, copy_function=shutil.copy2)
        if template.exists() and not template.is_dir():
            template.unlink()
        if template.exists():
            trash = root / f".trash-{template.name}-{os.getpid()}"
            shutil.rmtree(trash, ignore_errors=True)
            os.replace(template, trash)
            try:
                os.replace(staging, template)
            except OSError:
                if not template.exists():
                    os.replace(trash, template)
                raise
            shutil.rmtree(trash, ignore_errors=True)
        else:
            os.replace(staging, template)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _remove_stale(root: Path, key: str) -> None:
    """Remove staging/trash directories left by a builder that died."""
    for prefix in (".staging-", ".trash-"):
        for stale in root.glob(f"{prefix}{key}-*"):
            shutil.rmtree(stale, ignore_errors=True)


def directory_size(path: Path) -> int:
    """Approximate on-disk size of a directory tree, in bytes."""
    total = 0
    for entry in path.rglob("*"):
        with contextlib.suppress(OSError):
            if entry.is_file() and not entry.is_symlink():
                total += entry.stat().st_size
    return total


def prune_templates(
    root: Path,
    *,
    keep_key: str,
    max_bytes: int,
    grace_seconds: float = PRUNE_GRACE_SECONDS,
) -> list[str]:
    """Remove least-recently-published templates until the cap is met.

    The template that was just published is never removed, and neither is one
    touched within ``grace_seconds`` (another worker may be copying it).
    Returns the removed keys. ``max_bytes <= 0`` disables pruning.
    """
    if max_bytes <= 0 or not root.is_dir():
        return []
    entries = [
        entry
        for entry in root.iterdir()
        if entry.is_dir()
        and not entry.name.startswith(".")
        and (entry / "base").is_dir()
    ]
    if not entries:
        return []
    sizes = {entry: directory_size(entry) for entry in entries}
    total = sum(sizes.values())
    if total <= max_bytes:
        return []
    now = time.time()
    removed: list[str] = []
    for entry in sorted(entries, key=lambda item: item.stat().st_mtime):
        if total <= max_bytes:
            break
        if entry.name == keep_key:
            continue
        try:
            if now - entry.stat().st_mtime < grace_seconds:
                continue
        except OSError:
            continue
        shutil.rmtree(entry, ignore_errors=True)
        with contextlib.suppress(OSError):
            (root / f"{entry.name}.lock").unlink(missing_ok=True)
        total -= sizes[entry]
        removed.append(entry.name)
    return removed
