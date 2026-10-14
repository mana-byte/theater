"""Deterministic release evidence and rolling-branch reuse."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Collection, Sequence
from pathlib import Path
from typing import Any

from .contract import EVIDENCE_ROOT, Result, evidence_file, is_stable
from .inputs import adapter_digest

__all__ = ["adapter_digest", "reusable_evidence", "write_evidence"]


def _branch_evidence(root: Path, branch: str, harness: str, version: str) -> str | None:
    relative = EVIDENCE_ROOT / harness / f"{version}.json"
    result = subprocess.run(
        ["git", "show", f"{branch}:{relative.as_posix()}"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return result.stdout if result.returncode == 0 else None


def _proved(document: dict[str, Any], required: Sequence[str], skips: Collection[str]) -> bool:
    """Every required proof is recorded once as a pass (or a declared skip); nothing failed."""
    checks = document.get("checks")
    if not isinstance(checks, list) or not required:
        return False
    status: dict[str, object] = {}
    for check in checks:
        if not isinstance(check, dict) or not isinstance(check.get("name"), str):
            return False
        if check["name"] in status:
            return False
        status[check["name"]] = check.get("status")
    if any(value in ("fail", "error") for value in status.values()):
        return False
    return all(
        status.get(selector) == "pass" or (status.get(selector) == "skip" and selector in skips)
        for selector in required
    )


def _valid(
    document: Any,
    harness: str,
    version: str,
    digest: str,
    required: Sequence[str],
    skips: Collection[str],
) -> bool:
    return (
        isinstance(document, dict)
        and document.get("harness") == harness
        and document.get("version") == version
        and document.get("adapter_digest") == digest
        and document.get("verdict") == "green"
        and _proved(document, required, skips)
    )


def reusable_evidence(
    root: Path,
    harness: str,
    digest: str,
    *,
    branch: str | None = None,
    required: Sequence[str] = (),
    skips: Collection[str] = (),
) -> dict[str, dict[str, Any]]:
    """Green evidence on main or a fetched rolling branch that proves every required test."""
    versions: dict[str, dict[str, Any]] = {}
    directory = root / EVIDENCE_ROOT / harness
    if directory.is_dir():
        for path in sorted(directory.glob("*.json")):
            if is_stable(path.stem):
                try:
                    document = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if _valid(document, harness, path.stem, digest, required, skips):
                    versions[path.stem] = document
    if branch:
        listing = subprocess.run(
            ["git", "ls-tree", "-r", "--name-only", branch, "--", str(EVIDENCE_ROOT / harness)],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if listing.returncode == 0:
            for name in listing.stdout.splitlines():
                version = Path(name).stem
                if not is_stable(version) or version in versions:
                    continue
                raw = _branch_evidence(root, branch, harness, version)
                if raw is None:
                    continue
                try:
                    document = json.loads(raw)
                except ValueError:
                    continue
                if _valid(document, harness, version, digest, required, skips):
                    versions[version] = document
    return versions


def write_evidence(root: Path, result: Result, run_url: str) -> Path:
    path = evidence_file(root, result.harness, result.version)
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {**result.to_evidence(), "run_url": run_url}
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path
