"""Deterministic release evidence and rolling-branch reuse."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

from .contract import EVIDENCE_ROOT, Result, evidence_file, is_stable


def adapter_digest(root: Path, harness: str) -> str:
    """Hash only the frozen contract and this harness's adapter source files."""
    adapters = root / "scripts/harness_compat/adapters"
    module = adapters / f"{harness}.py"
    package = adapters / harness
    paths = [root / "scripts/harness_compat/contract.py"]
    if module.is_file():
        paths.append(module)
    if package.is_dir():
        paths.extend(sorted(package.rglob("*.py")))
    if len(paths) == 1:
        raise FileNotFoundError(f"adapter source absent: {harness}")
    digest = hashlib.sha256()
    for path in sorted(paths):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


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


def _valid(document: Any, harness: str, version: str, digest: str) -> bool:
    return (
        isinstance(document, dict)
        and document.get("harness") == harness
        and document.get("version") == version
        and document.get("adapter_digest") == digest
        and document.get("verdict") == "green"
        and isinstance(document.get("checks"), list)
    )


def reusable_evidence(
    root: Path, harness: str, digest: str, *, branch: str | None = None
) -> dict[str, dict[str, Any]]:
    """Read green evidence on main and a fetched rolling branch with this digest."""
    versions: dict[str, dict[str, Any]] = {}
    directory = root / EVIDENCE_ROOT / harness
    if directory.is_dir():
        for path in sorted(directory.glob("*.json")):
            if is_stable(path.stem):
                try:
                    document = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if _valid(document, harness, path.stem, digest):
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
                if _valid(document, harness, version, digest):
                    versions[version] = document
    return versions


def write_evidence(root: Path, result: Result, run_url: str) -> Path:
    path = evidence_file(root, result.harness, result.version)
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {**result.to_evidence(), "run_url": run_url}
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path
