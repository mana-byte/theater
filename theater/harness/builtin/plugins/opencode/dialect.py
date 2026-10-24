"""Verified OpenCode binaries and isolated database lineages."""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from pathlib import Path

from theater import paths
from theater.harness.contracts import version_probe
from theater.harness.contracts.version_policy import version_key
from theater.models import BadRequest

from .constants import LINEAGE_DB_NAME, LINEAGE_MARKER_NAME, MODELS_TIMEOUT
from .runtime_plan import _VERSION, VERSION_POLICY, parse_opencode_version, release_text

logger = logging.getLogger("theater.harness.opencode")


#: Pins the release Theater plans for, when a wrapper hides `opencode --version` (and in tests).
VERSION_ENV = "THEATER_OPENCODE_VERSION"


def resolve_binary(binary: str = "opencode") -> str:
    """The exact absolute executable a launch will run, or an actionable refusal.

    Pinning the resolved path keeps a PATH mismatch from running a different
    major against a shared database."""
    resolved = shutil.which(binary)
    if resolved is None:
        raise BadRequest(
            f"{binary!r} is not on PATH. Install a qualified OpenCode release "
            f"({VERSION_POLICY.range_text}) or fix PATH before "
            "spawning, or resume the session outside Theater."
        )
    return str(Path(resolved).resolve())


def installed_version(binary: str = "opencode") -> tuple[int, int, int] | None:
    """The verified release on PATH, or the explicit wrapper pin."""
    pinned = os.environ.get(VERSION_ENV)
    if pinned:
        return parse_opencode_version(pinned)
    try:
        version = version_probe.probe_release(binary, _VERSION, timeout=MODELS_TIMEOUT)
        return version_key(version) if version is not None else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def require_supported_version(binary: str = "opencode") -> tuple[int, int, int]:
    """Refuse unsupported releases before any launch or database access."""
    version = installed_version(binary)
    if version is None:
        raise BadRequest(
            f"could not verify the release of {binary!r}: `{binary} --version` reported no "
            "usable OpenCode release. Refusing to plan a launch against an unverified binary. "
            f"Install a qualified release ({VERSION_POLICY.range_text}) and put it on PATH, "
            "or pin the release explicitly "
            f"with {VERSION_ENV}."
        )
    rendered = release_text(version)
    if not VERSION_POLICY.admits(rendered):
        raise BadRequest(
            f"{binary} on PATH is OpenCode {rendered}; qualified releases are "
            f"{VERSION_POLICY.range_text}. "
            "Install a supported release or put one first on PATH, then retry the spawn."
        )
    return version


def participant_database_path(participant_id: str) -> Path:
    """A 2.x participant's own database, the root of its resume lineage."""
    return paths.participant_observation_dir(participant_id, "opencode") / LINEAGE_DB_NAME


def lineage_marker(participant_id: str) -> tuple[Path, str]:
    """A marker beside the 2.x database: names a native spawn, whose domain no plan records.

    Written with the launch files, it also creates the directory SQLite will not create.
    """
    marker = participant_database_path(participant_id).parent / LINEAGE_MARKER_NAME
    return marker, json.dumps({"participant_id": participant_id, "database": LINEAGE_DB_NAME})


def has_isolated_database(participant_id: str) -> bool:
    return lineage_marker(participant_id)[0].exists()


def is_lineage_database(db: Path) -> bool:
    return db.name == LINEAGE_DB_NAME


def database_for_domain(domain: str | None) -> Path | None:
    """The isolated database a transcript domain names, or None for a foreign domain."""
    if not domain or not domain.startswith("opencode://"):
        return None
    db = Path(domain.removeprefix("opencode://"))
    return db if db.is_absolute() and is_lineage_database(db) else None


def domain_for(db: Path) -> str:
    return f"opencode://{db.expanduser().resolve()}"


__all__ = [
    "database_for_domain",
    "domain_for",
    "has_isolated_database",
    "installed_version",
    "is_lineage_database",
    "lineage_marker",
    "participant_database_path",
    "require_supported_version",
    "resolve_binary",
]
