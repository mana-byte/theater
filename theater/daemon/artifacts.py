"""Participant-owned launch artifacts and filesystem cleanup."""

from __future__ import annotations

import logging
import re
import shutil
import stat
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from theater import paths
from theater.harness.contracts.launch import LaunchPlan
from theater.models import BadRequest, Participant

_CANONICAL_ID = re.compile(r"^[0-9a-f]{12}$")
logger = logging.getLogger("theater.artifacts")


class ArtifactKind(StrEnum):
    FILE = "file"
    DIRECTORY = "directory"


@dataclass(frozen=True, slots=True)
class OwnedArtifact:
    path: Path
    kind: ArtifactKind

    def __post_init__(self) -> None:
        if not isinstance(self.path, Path):
            raise TypeError("owned artifact path must be a Path")
        if not isinstance(self.kind, ArtifactKind):
            raise TypeError("owned artifact kind must be an ArtifactKind")


def artifacts_for_plan(plan: LaunchPlan, participant: Participant) -> tuple[OwnedArtifact, ...]:
    found: dict[Path, ArtifactKind] = {}

    def add(path: Path, kind: ArtifactKind) -> None:
        normalized = validate_owned_path(
            path,
            participant_id=participant.id,
            kind=kind,
        )
        previous = found.get(normalized)
        if previous is not None and previous is not kind:
            raise BadRequest(f"launch artifacts collide at {path!r}")
        found[normalized] = kind

    add(paths.participant_dir(participant.id), ArtifactKind.DIRECTORY)
    add(paths.mcp_config_path(participant.id), ArtifactKind.FILE)
    add(
        paths.participant_observation_dir(participant.id, participant.harness),
        ArtifactKind.DIRECTORY,
    )
    for path in (*plan.files, *plan.private_files):
        add(path, ArtifactKind.FILE)
    if plan.receipt_token_path is not None:
        add(plan.receipt_token_path, ArtifactKind.FILE)
    for credential in plan.channel_credentials:
        add(credential.token_path, ArtifactKind.FILE)
    return tuple(OwnedArtifact(path, kind) for path, kind in sorted(found.items()))


def baseline_artifacts(participant: Participant) -> tuple[OwnedArtifact, ...]:
    return (OwnedArtifact(paths.participant_dir(participant.id), ArtifactKind.DIRECTORY),)


def remove_secret_file(path: str | Path, *, owner_id: str) -> None:
    candidate = Path(path)
    try:
        safe_path = validate_persisted_path(candidate, owner_id=owner_id, kind=ArtifactKind.FILE)
    except ValueError as exc:
        logger.warning("refusing to remove secret artifact %s: %s", candidate, exc)
        return
    try:
        safe_path.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("leaving secret artifact %s: %s", safe_path, exc)


def validate_owned_path(
    path: Path,
    *,
    participant_id: str,
    kind: ArtifactKind,
) -> Path:
    try:
        return _validate_path(path, owner_id=participant_id, kind=kind)
    except (TypeError, ValueError) as exc:
        raise BadRequest(str(exc)) from exc


def validate_persisted_path(path: Path, *, owner_id: str, kind: ArtifactKind) -> Path:
    return _validate_path(path, owner_id=owner_id, kind=kind)


def _validate_path(path: Path, *, owner_id: str, kind: ArtifactKind) -> Path:
    if not isinstance(path, Path) or not path.is_absolute():
        raise ValueError(f"participant artifact path {path!r} must be absolute")
    if not isinstance(kind, ArtifactKind):
        raise TypeError("participant artifact kind is invalid")
    try:
        owner_root = paths.participant_dir(owner_id)
    except ValueError as exc:
        raise ValueError("participant artifact owner contains an unsafe path component") from exc
    if any(part in {".", ".."} for part in path.parts):
        raise ValueError(f"participant artifact path {path!r} contains a relative component")
    if _contains_symlink(owner_root, paths.home()) or _path_contains_symlink(path, owner_root):
        raise ValueError(f"participant artifact path {path!r} contains a symlink")
    try:
        candidate = path.resolve(strict=False)
        resolved_root = owner_root.resolve(strict=False)
        candidate.relative_to(resolved_root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError(f"participant artifact path {path!r} escapes its owner root") from exc
    if kind is ArtifactKind.FILE and candidate == resolved_root:
        raise ValueError("participant file artifact must be below its owner root")
    return candidate


def cleanup_participant(
    participant: Participant,
    recorded: tuple[OwnedArtifact, ...],
) -> tuple[str, ...]:
    artifacts = [*recorded, *baseline_artifacts(participant)]
    return _remove_artifacts(artifacts, owner_id=participant.id)


def cleanup_orphan_recorded(
    owner_id: str,
    recorded: tuple[OwnedArtifact, ...],
) -> tuple[str, ...]:
    return _remove_artifacts(recorded, owner_id=owner_id)


def orphan_paths(retained_ids: frozenset[str]) -> tuple[tuple[OwnedArtifact, str], ...]:
    found: list[tuple[OwnedArtifact, str]] = []
    for participant_root in _children(paths.participants_dir()):
        owner_id = participant_root.name
        if (
            _CANONICAL_ID.fullmatch(owner_id)
            and owner_id not in retained_ids
            and participant_root.is_dir()
            and not participant_root.is_symlink()
        ):
            found.append((OwnedArtifact(participant_root, ArtifactKind.DIRECTORY), owner_id))
    return tuple(found)


def cleanup_orphan_paths(
    candidates: tuple[tuple[OwnedArtifact, str], ...],
) -> tuple[str, ...]:
    failures: list[str] = []
    for artifact, owner_id in candidates:
        failures.extend(_remove_artifacts((artifact,), owner_id=owner_id))
    return tuple(failures)


def _remove_artifacts(
    artifacts: list[OwnedArtifact] | tuple[OwnedArtifact, ...],
    *,
    owner_id: str,
) -> tuple[str, ...]:
    unique: dict[Path, ArtifactKind] = {}
    failures: list[str] = []
    for artifact in artifacts:
        try:
            path = validate_persisted_path(
                artifact.path,
                owner_id=owner_id,
                kind=artifact.kind,
            )
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            failures.append(f"{artifact.path!s}: {exc}")
            continue
        previous = unique.get(path)
        if previous is ArtifactKind.DIRECTORY or artifact.kind is previous:
            continue
        unique[path] = artifact.kind

    ordered = sorted(
        (OwnedArtifact(path, kind) for path, kind in unique.items()),
        key=lambda item: (
            item.kind is ArtifactKind.DIRECTORY,
            -len(item.path.parts),
            str(item.path),
        ),
    )
    for artifact in ordered:
        try:
            _remove_one(artifact)
        except OSError as exc:
            failures.append(f"{artifact.path!s}: {exc}")
    return tuple(failures)


def _remove_one(artifact: OwnedArtifact) -> None:
    try:
        mode = artifact.path.lstat().st_mode
    except FileNotFoundError:
        return
    if stat.S_ISLNK(mode):
        raise OSError("refusing to remove a symlink")
    if artifact.kind is ArtifactKind.FILE:
        if not stat.S_ISREG(mode):
            raise OSError("expected a regular file")
        artifact.path.unlink()
        return
    if not stat.S_ISDIR(mode):
        raise OSError("expected a directory")
    shutil.rmtree(artifact.path)


def _children(root: Path) -> tuple[Path, ...]:
    try:
        return tuple(root.iterdir())
    except FileNotFoundError:
        return ()
    except OSError as exc:
        logger.warning("cannot inspect participant root %s: %s", root, exc)
        return ()


def _contains_symlink(path: Path, root: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    current = root
    for part in relative.parts:
        current /= part
        try:
            if current.is_symlink():
                return True
        except OSError:
            return True
    return False


def _path_contains_symlink(path: Path, owner_root: Path) -> bool:
    if path.is_relative_to(owner_root):
        return _contains_symlink(path, owner_root)
    resolved_root = owner_root.resolve(strict=False)
    if path.is_relative_to(resolved_root):
        return _contains_symlink(path, resolved_root)
    return False


__all__ = [
    "ArtifactKind",
    "OwnedArtifact",
    "artifacts_for_plan",
    "baseline_artifacts",
    "cleanup_orphan_paths",
    "cleanup_orphan_recorded",
    "cleanup_participant",
    "orphan_paths",
    "remove_secret_file",
    "validate_owned_path",
    "validate_persisted_path",
]
