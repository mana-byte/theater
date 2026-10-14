"""Digest-validated release evidence stored outside the checkout."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Collection, Mapping, Sequence
from pathlib import Path
from typing import Any

from .artifacts import valid_artifact_path
from .contract import Result, is_stable
from .inputs import adapter_digest

__all__ = [
    "adapter_digest",
    "cache_artifacts_dir",
    "cache_root",
    "reusable_evidence",
    "write_evidence",
]


def cache_root(root: Path) -> Path:
    override = os.environ.get("HARNESS_COMPAT_CACHE")
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = (
        Path(override)
        if override
        else Path(xdg) / "theater/harness-compat"
        if xdg
        else (Path.home() / ".cache/theater/harness-compat")
    )
    cache = base.expanduser().resolve()
    if cache.is_relative_to(root.resolve()):
        raise ValueError("HARNESS_COMPAT_CACHE must be outside the repository")
    return cache


def cache_artifacts_dir(root: Path, harness: str, version: str) -> Path:
    return cache_root(root) / harness / "artifacts" / version


def _proved(document: dict[str, Any], required: Sequence[str], skips: Collection[str]) -> bool:
    """Every required proof is recorded once as a pass or a declared skip."""
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


def _artifacts_valid(directory: Path, document: dict[str, Any]) -> bool:
    artifacts = document.get("artifacts", {})
    if not isinstance(artifacts, dict):
        return False
    for name, expected in artifacts.items():
        if not isinstance(name, str) or not isinstance(expected, str):
            return False
        relative = Path(name)
        if not valid_artifact_path(name):
            return False
        path = directory / relative
        if not path.is_file() or not path.resolve().is_relative_to(directory.resolve()):
            return False
        try:
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            return False
        if actual != expected:
            return False
    return True


def _valid(
    document: Any,
    harness: str,
    version: str,
    digest: str,
    required: Sequence[str],
    skips: Collection[str],
    artifacts: Path,
) -> bool:
    return (
        isinstance(document, dict)
        and document.get("harness") == harness
        and document.get("version") == version
        and document.get("adapter_digest") == digest
        and document.get("verdict") == "green"
        and _proved(document, required, skips)
        and _artifacts_valid(artifacts, document)
    )


def reusable_evidence(
    root: Path,
    harness: str,
    digest: str,
    *,
    required: Sequence[str] = (),
    skips: Collection[str] = (),
) -> dict[str, dict[str, Any]]:
    """Green cached results whose digest, proofs, and artifacts still match."""
    versions: dict[str, dict[str, Any]] = {}
    directory = cache_root(root) / harness
    if directory.is_dir():
        for path in sorted(directory.glob("*.json")):
            if is_stable(path.stem):
                try:
                    document = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if _valid(
                    document,
                    harness,
                    path.stem,
                    digest,
                    required,
                    skips,
                    directory / "artifacts" / path.stem,
                ):
                    versions[path.stem] = document
    return versions


def write_evidence(
    root: Path, result: Result, run_url: str, *, artifacts: Mapping[str, str] | None = None
) -> Path:
    if result.verdict != "green" or not result.adapter_digest or not is_stable(result.version):
        raise ValueError("only digest-stamped green stable releases can be cached")
    path = cache_root(root) / result.harness / f"{result.version}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {**result.to_evidence(), "run_url": run_url, "artifacts": dict(artifacts or {})}
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)
    return path
