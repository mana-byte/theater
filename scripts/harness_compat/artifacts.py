"""Carry green candidate files into the composed tree and stage its exact changes."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from collections.abc import Collection
from pathlib import Path

from .contract import EVIDENCE_ROOT


def valid_artifact_path(name: str) -> bool:
    relative = Path(name)
    return (
        bool(name)
        and not relative.is_absolute()
        and ".." not in relative.parts
        and not relative.is_relative_to(EVIDENCE_ROOT)
    )


def _git(root: Path, *args: str) -> bytes:
    result = subprocess.run(["git", *args], cwd=root, capture_output=True, timeout=60, check=False)
    if result.returncode:
        raise RuntimeError(f"git {' '.join(args)} failed: {os.fsdecode(result.stderr).strip()}")
    return result.stdout


def _paths(raw: bytes) -> set[Path]:
    return {Path(os.fsdecode(value)) for value in raw.split(b"\0") if value}


def untracked_files(root: Path) -> set[Path]:
    """List each non-ignored untracked file, including files inside new directories."""
    return _paths(_git(root, "ls-files", "--others", "--exclude-standard", "-z"))


def status_untracked(root: Path) -> set[Path]:
    """Read untracked files from porcelain status after an adapter bump."""
    entries = _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all").split(b"\0")
    files: set[Path] = set()
    index = 0
    while index < len(entries):
        entry = entries[index]
        if entry.startswith(b"?? "):
            files.add(Path(os.fsdecode(entry[3:])))
        if len(entry) >= 2 and (b"R" in entry[:2] or b"C" in entry[:2]):
            index += 1
        index += 1
    return files


def tracked_changes(root: Path) -> set[Path]:
    return _paths(_git(root, "diff", "--name-only", "-z", "HEAD", "--"))


def tracked_patch(root: Path) -> bytes:
    return _git(root, "diff", "--binary", "HEAD", "--")


def _copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination, follow_symlinks=False)


class CandidateArtifacts:
    """One candidate's files never overwrite another candidate's proof."""

    def __init__(self, storage: Path) -> None:
        self.storage = storage
        self.owners: dict[Path, str] = {}

    def capture(self, tree: Path, version: str) -> list[str]:
        problems: list[str] = []
        for relative in sorted(untracked_files(tree)):
            if not valid_artifact_path(relative.as_posix()):
                problems.append(f"candidate wrote forbidden evidence path: {relative}")
                continue
            if owner := self.owners.get(relative):
                problems.append(
                    f"candidate artifact {relative} conflicts between {owner} and {version}"
                )
                continue
            self.owners[relative] = version
            _copy(tree / relative, self.storage / relative)
        return problems

    def cache_version(self, version: str, directory: Path) -> dict[str, str]:
        """Persist one green candidate's generated files outside the checkout."""
        manifest: dict[str, str] = {}
        for relative, owner in sorted(self.owners.items()):
            if owner != version:
                continue
            source = self.storage / relative
            target = directory / relative
            _copy(source, target)
            manifest[relative.as_posix()] = hashlib.sha256(source.read_bytes()).hexdigest()
        return manifest

    def add_cached(self, version: str, directory: Path, manifest: dict[str, str]) -> None:
        """Restore validated generated files for a reused release."""
        for name, digest in sorted(manifest.items()):
            relative = Path(name)
            if not valid_artifact_path(name):
                raise ValueError(f"invalid cached artifact path: {name!r}")
            if relative in self.owners:
                raise ValueError(f"cached artifact conflicts with {self.owners[relative]}: {name}")
            source = directory / relative
            if not source.resolve().is_relative_to(directory.resolve()):
                raise ValueError(f"cached artifact escapes cache: {name}")
            if hashlib.sha256(source.read_bytes()).hexdigest() != digest:
                raise ValueError(f"cached artifact changed: {name}")
            _copy(source, self.storage / relative)
            self.owners[relative] = version

    def copy_into(self, tree: Path, versions: Collection[str]) -> None:
        """Only promoted releases' files; a captured green that is not promoted must not ship."""
        for relative in sorted(self.owners):
            if self.owners[relative] not in versions:
                continue
            target = tree / relative
            if target.exists() or target.is_symlink():
                raise RuntimeError(f"artifact path already exists in composed tree: {relative}")
            _copy(self.storage / relative, target)

    def stray_files(self, tree: Path, changed: list[Path]) -> list[str]:
        untracked = status_untracked(tree)
        expected = set(self.owners) | set(changed)
        unexpected = {
            item
            for item in untracked
            if item not in expected and not any(parent in expected for parent in item.parents)
        }
        leftovers = {
            item for item in untracked if "candidates" in item.parts and item in self.owners
        }
        return [
            f"stray untracked file after bump: {item}" for item in sorted(unexpected | leftovers)
        ]


def stage_composed(tree: Path, root: Path) -> list[Path]:
    """Copy only the composed tree's Git changes to the checkout and stage those paths."""
    if _git(root, "status", "--porcelain=v1", "-z"):
        raise RuntimeError("publishing checkout is dirty before staging the composed tree")
    changed = tracked_changes(tree) | status_untracked(tree)
    if forbidden := [path for path in changed if not valid_artifact_path(path.as_posix())]:
        raise RuntimeError(f"composed tree contains forbidden evidence paths: {sorted(forbidden)}")
    for relative in sorted(changed):
        source, target = tree / relative, root / relative
        if source.exists() or source.is_symlink():
            _copy(source, target)
        elif target.exists() or target.is_symlink():
            target.unlink()
    ordered = sorted(changed)
    for start in range(0, len(ordered), 100):
        batch = ordered[start : start + 100]
        _git(root, "add", "-A", "--", *(path.as_posix() for path in batch))
    staged = _paths(_git(root, "diff", "--cached", "--name-only", "-z", "--"))
    if staged != changed:
        raise RuntimeError(f"staged paths differ from composed tree: {sorted(staged ^ changed)}")
    return ordered
