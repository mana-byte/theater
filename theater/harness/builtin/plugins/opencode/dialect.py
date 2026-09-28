"""OpenCode major-release dialects: 1.x and 2.x differ in CLI, storage, and plugin shapes."""

from __future__ import annotations

import enum
import json
import logging
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

from theater import paths
from theater.models import BadRequest

from .constants import MODELS_TIMEOUT, V2_DB_NAME, V2_MARKER_NAME
from .runtime_plan import parse_opencode_version

logger = logging.getLogger("theater.harness.opencode")


class OpenCodeDialect(enum.StrEnum):
    V1 = "v1"
    V2 = "v2"


#: Pins the release Theater plans for, when a wrapper hides `opencode --version` (and in tests).
VERSION_ENV = "THEATER_OPENCODE_VERSION"

_MAJORS = {1: OpenCodeDialect.V1, 2: OpenCodeDialect.V2}
#: Successful and failed probes, keyed by resolved file identity (path, mtime, size):
#: a repaired or replaced CLI re-probes, so a failure never permanently poisons it.
_versions: dict[tuple[str, int, int], tuple[tuple[int, int, int] | None, float]] = {}
_VERSION_CACHE_MAX = 128
#: A failed probe retries after this long even when the file is unchanged: a
#: transient timeout must not poison every spawn until the daemon restarts.
_FAILURE_CACHE_SECONDS = 2.0
_lock = threading.Lock()


def resolve_binary(binary: str = "opencode") -> str:
    """The exact absolute executable a launch will run, or an actionable refusal.

    Pinning the resolved path keeps a PATH mismatch from running a different
    major against a shared database."""
    resolved = shutil.which(binary)
    if resolved is None:
        raise BadRequest(
            f"{binary!r} is not on PATH. Install OpenCode 1.x or 2.x (or fix PATH) before "
            "spawning, or resume the session outside Theater."
        )
    return str(Path(resolved).resolve())


def dialect_for_version(version: tuple[int, int, int] | None) -> OpenCodeDialect | None:
    return None if version is None else _MAJORS.get(version[0])


def installed_version(binary: str = "opencode") -> tuple[int, int, int] | None:
    """The release on PATH, cached per binary file so launch planning probes it once.

    Failures cache too, but expire: the key carries mtime and size, so a
    repaired CLI re-probes immediately."""
    pinned = os.environ.get(VERSION_ENV)
    if pinned:
        return parse_opencode_version(pinned)
    resolved = shutil.which(binary)
    if resolved is None:
        return None
    real = Path(resolved).resolve()
    try:
        info = real.stat()
    except OSError:
        return None
    key = (str(real), info.st_mtime_ns, info.st_size)
    with _lock:
        cached = _versions.get(key)
        if cached is not None:
            version, probed_at = cached
            fresh_failure = version is None and (
                time.monotonic() - probed_at < _FAILURE_CACHE_SECONDS
            )
            if version is not None or fresh_failure:
                return version
    try:
        run = subprocess.run(
            [resolved, "--version"],
            capture_output=True,
            text=True,
            timeout=MODELS_TIMEOUT,
            check=False,
        )
        version = (
            parse_opencode_version(f"{run.stdout}\n{run.stderr}") if run.returncode == 0 else None
        )
    except (OSError, subprocess.SubprocessError):
        version = None
    with _lock:
        if len(_versions) >= _VERSION_CACHE_MAX:
            _versions.pop(next(iter(_versions)))
        _versions[key] = (version, time.monotonic())
    return version


def installed_dialect(binary: str = "opencode") -> OpenCodeDialect:
    """The verified dialect of the OpenCode a launch will run, never a default.

    An unreadable release is refused: a wrong-major binary can erase a shared
    database's event log. An explicit ``THEATER_OPENCODE_VERSION`` pin supplies
    the release (tests, wrappers)."""
    version = installed_version(binary)
    if version is None:
        raise BadRequest(
            f"could not verify the release of {binary!r}: `{binary} --version` reported no "
            "usable OpenCode release. Refusing to plan a launch against an unverified binary. "
            "Install OpenCode 1.x or 2.x and put it on PATH, or pin the release explicitly "
            f"with {VERSION_ENV}."
        )
    dialect = dialect_for_version(version)
    if dialect is None:
        rendered = ".".join(str(part) for part in version)
        raise BadRequest(
            f"{binary} on PATH is OpenCode {rendered}; Theater drives OpenCode 1.x and 2.x only. "
            "Install a supported release or put one first on PATH, then retry the spawn."
        )
    return dialect


def v2_database_path(participant_id: str) -> Path:
    """A 2.x participant's own database, the root of its resume lineage."""
    return paths.participant_observation_dir(participant_id, "opencode") / V2_DB_NAME


def v2_lineage_marker(participant_id: str) -> tuple[Path, str]:
    """A marker beside the 2.x database: names a native spawn, whose domain no plan records.

    Written with the launch files, it also creates the directory SQLite will not create.
    """
    marker = v2_database_path(participant_id).parent / V2_MARKER_NAME
    return marker, json.dumps({"participant_id": participant_id, "database": V2_DB_NAME})


def is_v2_participant(participant_id: str) -> bool:
    return v2_lineage_marker(participant_id)[0].exists()


def is_v2_database(db: Path) -> bool:
    return db.name == V2_DB_NAME


def v2_database_for_domain(domain: str | None) -> Path | None:
    """The 2.x database a transcript domain names, or None for a 1.x or foreign domain."""
    if not domain or not domain.startswith("opencode://"):
        return None
    db = Path(domain.removeprefix("opencode://"))
    return db if db.is_absolute() and is_v2_database(db) else None


def domain_for(db: Path) -> str:
    return f"opencode://{db.expanduser().resolve()}"


__all__ = [
    "OpenCodeDialect",
    "dialect_for_version",
    "domain_for",
    "installed_dialect",
    "installed_version",
    "is_v2_database",
    "is_v2_participant",
    "resolve_binary",
    "v2_database_for_domain",
    "v2_database_path",
    "v2_lineage_marker",
]
