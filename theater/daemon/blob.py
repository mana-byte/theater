"""Git blob hashing (``sha1(b"blob %d\0" % len(data) + data)``) without invoking git.
In-process is ~900x faster than ``git hash-object`` per job. It ignores .gitattributes filters,
which is safe only because we compare our hashes to our own, never to git's.
"""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from theater.constants.daemon import TOUCH_HASH_CHUNK_BYTES, TOUCH_HASH_MAX_FILE_BYTES


class BlobHashState(StrEnum):
    """Whether a path was hashed, absent, or unsafe to classify."""

    HASHED = "hashed"
    MISSING = "missing"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class BlobHash:
    """Bounded hash outcome. Only ``MISSING`` maps to creation/deletion."""

    state: BlobHashState
    digest: str | None = None
    size: int = 0
    reason: str | None = None


def blob_hash(path: Path, *, max_bytes: int = TOUCH_HASH_MAX_FILE_BYTES) -> BlobHash:
    """Hash one stable regular file without following links or reading without bound."""
    if max_bytes < 0:
        raise ValueError("max_bytes must be non-negative")

    try:
        before = path.lstat()
    except (FileNotFoundError, NotADirectoryError):
        return BlobHash(BlobHashState.MISSING)
    except (OSError, ValueError):
        return BlobHash(BlobHashState.UNAVAILABLE, reason="stat_failed")
    if not stat.S_ISREG(before.st_mode):
        return BlobHash(BlobHashState.UNAVAILABLE, reason="not_regular")
    if before.st_size > max_bytes:
        return BlobHash(BlobHashState.UNAVAILABLE, reason="too_large")

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except (FileNotFoundError, NotADirectoryError):
        return BlobHash(BlobHashState.MISSING)
    except (OSError, ValueError):
        return BlobHash(BlobHashState.UNAVAILABLE, reason="open_failed")

    try:
        opened = os.fstat(fd)
        return _hash_open_regular_file(path, fd, before, opened, max_bytes=max_bytes)
    except OSError:
        return BlobHash(BlobHashState.UNAVAILABLE, reason="read_failed")
    finally:
        os.close(fd)


def _hash_open_regular_file(path: Path, fd: int, before, opened, *, max_bytes: int) -> BlobHash:
    if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (
        before.st_dev,
        before.st_ino,
    ):
        return BlobHash(BlobHashState.UNAVAILABLE, reason="path_changed")
    if opened.st_size > max_bytes:
        return BlobHash(BlobHashState.UNAVAILABLE, reason="too_large")

    digest = hashlib.sha1(b"blob %d\0" % opened.st_size)
    remaining = opened.st_size
    while remaining:
        chunk = os.read(fd, min(TOUCH_HASH_CHUNK_BYTES, remaining))
        if not chunk:
            return BlobHash(BlobHashState.UNAVAILABLE, reason="changed_while_reading")
        digest.update(chunk)
        remaining -= len(chunk)
    if os.read(fd, 1):
        return BlobHash(BlobHashState.UNAVAILABLE, reason="changed_while_reading")

    after = os.fstat(fd)
    try:
        path_after = path.lstat()
    except OSError:
        return BlobHash(BlobHashState.UNAVAILABLE, reason="path_changed")
    stable_fields = ("st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns")
    if any(getattr(after, field) != getattr(opened, field) for field in stable_fields):
        return BlobHash(BlobHashState.UNAVAILABLE, reason="changed_while_reading")
    if (path_after.st_dev, path_after.st_ino) != (opened.st_dev, opened.st_ino):
        return BlobHash(BlobHashState.UNAVAILABLE, reason="path_changed")
    return BlobHash(BlobHashState.HASHED, digest.hexdigest(), opened.st_size)


def blob_sha(path: Path) -> str | None:
    """Git's blob hash for ``path``, or ``None`` when no digest is available.
    Compatibility API that collapses missing and unavailable reads; touch indexing uses
    ``blob_hash``.
    """
    outcome = blob_hash(path)
    return outcome.digest if outcome.state is BlobHashState.HASHED else None
