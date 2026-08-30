"""Participant-owned launch artifacts and bounded filesystem cleanup."""

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
    """Validate and collect every file a launch plan can create."""
    found: dict[Path, ArtifactKind] = {}

    def add(path: Path, kind: ArtifactKind) -> None:
        normalized = validate_owned_path(
            path,
            participant_id=participant.id,
            harness=participant.harness,
            kind=kind,
        )
        previous = found.get(normalized)
        if previous is not None and previous is not kind:
            raise BadRequest(f"launch artifacts collide at {path!r}")
        found[normalized] = kind

    add(paths.mcp_config_path(participant.id), ArtifactKind.FILE)
    add(paths.observation_dir(participant.harness, participant.id), ArtifactKind.DIRECTORY)
    for path in plan.files:
        add(path, ArtifactKind.FILE)
    for path in plan.private_files:
        add(path, ArtifactKind.FILE)
    if plan.receipt_token_path is not None:
        add(plan.receipt_token_path, ArtifactKind.FILE)
    for credential in plan.channel_credentials:
        add(credential.token_path, ArtifactKind.FILE)
    return tuple(OwnedArtifact(path, kind) for path, kind in sorted(found.items()))


def baseline_artifacts(participant: Participant) -> tuple[OwnedArtifact, ...]:
    """Return the standard paths needed to retry cleanup after row deletion."""
    return (
        OwnedArtifact(
            paths.observation_dir(participant.harness, participant.id),
            ArtifactKind.DIRECTORY,
        ),
        OwnedArtifact(paths.mcp_config_path(participant.id), ArtifactKind.FILE),
        OwnedArtifact(paths.participant_artifacts_dir(participant.id), ArtifactKind.DIRECTORY),
    )


def remove_secret_file(path: str | Path, *, owner_id: str) -> None:
    """Remove a core-owned secret only from a validated Theater-owned root."""
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
    harness: str | None,
    kind: ArtifactKind,
) -> Path:
    """Return a canonical path only when it is in a participant-owned root."""
    if not isinstance(path, Path) or not path.is_absolute():
        raise BadRequest(f"participant artifact path {path!r} must be absolute")
    if not _safe_component(participant_id) or (
        harness is not None and not _safe_component(harness)
    ):
        raise BadRequest("participant artifact owner contains an unsafe path component")
    if any(part in {".", ".."} for part in path.parts):
        raise BadRequest(f"participant artifact path {path!r} contains a relative component")
    try:
        candidate = path.resolve(strict=False)
        home = paths.home().resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise BadRequest(f"cannot resolve participant artifact path {path!r}: {exc}") from exc
    try:
        candidate.relative_to(home)
    except ValueError:
        raise BadRequest(
            f"participant artifact path {path!r} must resolve under Theater home {home!r}"
        ) from None
    if _contains_symlink(path, home):
        raise BadRequest(f"participant artifact path {path!r} contains a symlink")

    try:
        mcp = paths.mcp_config_dir().resolve(strict=False)
        launch = paths.launch_artifacts_dir().resolve(strict=False)
        observations = paths.observations_dir().resolve(strict=False)
        participant_root = paths.participant_artifacts_dir(participant_id).resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise BadRequest(f"cannot resolve participant artifact roots: {exc}") from exc
    if kind is ArtifactKind.DIRECTORY:
        observation_root = (
            paths.observation_dir(harness, participant_id).resolve(strict=False)
            if harness is not None
            else None
        )
        if candidate not in {participant_root, observation_root}:
            raise BadRequest(f"participant directory artifact {path!r} is outside its owner root")
        return candidate

    if (
        _is_owned_direct_file(candidate, mcp, participant_id)
        or _is_owned_direct_file(candidate, launch, participant_id)
        or _is_observation_descendant(candidate, observations, participant_id, harness)
        or _is_descendant(candidate, participant_root, None)
    ):
        return candidate
    raise BadRequest(
        f"participant artifact path {path!r} is outside a declared Theater-owned artifact root"
    )


def validate_persisted_path(path: Path, *, owner_id: str, kind: ArtifactKind) -> Path:
    """Validate an owned path after its participant's harness is unavailable."""
    if not isinstance(path, Path) or not path.is_absolute() or not _safe_component(owner_id):
        raise ValueError(f"persisted participant artifact path {path!r} is invalid")
    if any(part in {".", ".."} for part in path.parts):
        raise ValueError(
            f"persisted participant artifact path {path!r} contains a relative component"
        )
    try:
        candidate = path.resolve(strict=False)
        home = paths.home().resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise ValueError(
            f"cannot resolve persisted participant artifact path {path!r}: {exc}"
        ) from exc
    try:
        candidate.relative_to(home)
    except ValueError:
        raise ValueError(
            f"persisted participant artifact path {path!r} is outside Theater home"
        ) from None
    if _contains_symlink(path, home):
        raise ValueError(f"persisted participant artifact path {path!r} contains a symlink")

    try:
        mcp = paths.mcp_config_dir().resolve(strict=False)
        launch = paths.launch_artifacts_dir().resolve(strict=False)
        observations = paths.observations_dir().resolve(strict=False)
        participant_root = paths.participant_artifacts_dir(owner_id).resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"cannot resolve persisted participant artifact roots: {exc}") from exc
    if kind is ArtifactKind.FILE and (
        _is_owned_direct_file(candidate, mcp, owner_id)
        or _is_owned_direct_file(candidate, launch, owner_id)
        or _is_observation_descendant(candidate, observations, owner_id)
        or _is_descendant(candidate, participant_root, None)
    ):
        return candidate
    if kind is ArtifactKind.DIRECTORY and (
        _is_observation_root(candidate, observations, owner_id) or candidate == participant_root
    ):
        return candidate
    raise ValueError(f"persisted participant artifact path {path!r} is outside its owner root")


def cleanup_participant(
    participant: Participant,
    recorded: tuple[OwnedArtifact, ...],
) -> tuple[str, ...]:
    """Delete one participant's artifacts; return failures for a later retry."""
    artifacts = list(recorded)
    try:
        artifacts.extend(baseline_artifacts(participant))
    except (TypeError, ValueError):
        return (f"participant {participant.id!r} has an unsafe generated artifact root",)

    if _CANONICAL_ID.fullmatch(participant.id):
        legacy_failures: list[str] = []
        for root in (paths.mcp_config_dir(), paths.launch_artifacts_dir()):
            legacy, failures = _prefixed_files(root, participant.id)
            artifacts.extend(OwnedArtifact(child, ArtifactKind.FILE) for child in legacy)
            legacy_failures.extend(failures)
        return tuple(
            legacy_failures
            + list(
                _remove_artifacts(artifacts, owner_id=participant.id, harness=participant.harness)
            )
        )
    return _remove_artifacts(artifacts, owner_id=participant.id, harness=participant.harness)


def cleanup_orphan_recorded(
    owner_id: str,
    recorded: tuple[OwnedArtifact, ...],
) -> tuple[str, ...]:
    """Delete recorded artifacts whose participant row is already gone."""
    return _remove_artifacts(recorded, owner_id=owner_id, harness=None)


def orphan_paths(retained_ids: frozenset[str]) -> tuple[tuple[OwnedArtifact, str], ...]:
    """Find legacy artifacts in dedicated roots without recursively globbing."""
    found: list[tuple[OwnedArtifact, str]] = []
    for root in (paths.mcp_config_dir(), paths.launch_artifacts_dir()):
        for child in _children(root):
            if not _is_regular_or_symlink(child):
                continue
            owner_id, _, _suffix = child.name.partition(".")
            if not _CANONICAL_ID.fullmatch(owner_id) or owner_id in retained_ids:
                continue
            found.append((OwnedArtifact(child, ArtifactKind.FILE), owner_id))

    observations = paths.observations_dir()
    for harness_root in _children(observations):
        if not harness_root.is_dir() or harness_root.is_symlink():
            continue
        for participant_root in _children(harness_root):
            owner_id = participant_root.name
            if (
                _CANONICAL_ID.fullmatch(owner_id)
                and owner_id not in retained_ids
                and participant_root.is_dir()
                and not participant_root.is_symlink()
            ):
                found.append((OwnedArtifact(participant_root, ArtifactKind.DIRECTORY), owner_id))

    artifacts_root = paths.home() / "artifacts"
    for participant_root in _children(artifacts_root):
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
    """Delete legacy candidates after their owner rows are absent."""
    failures: list[str] = []
    for artifact, owner_id in candidates:
        failures.extend(_remove_artifacts((artifact,), owner_id=owner_id, harness=None))
    return tuple(failures)


def _remove_artifacts(
    artifacts: list[OwnedArtifact] | tuple[OwnedArtifact, ...],
    *,
    owner_id: str,
    harness: str | None,
) -> tuple[str, ...]:
    unique: dict[Path, ArtifactKind] = {}
    failures: list[str] = []
    for artifact in artifacts:
        try:
            path = (
                validate_owned_path(
                    artifact.path,
                    participant_id=owner_id,
                    harness=harness,
                    kind=artifact.kind,
                )
                if harness is not None
                else validate_persisted_path(artifact.path, owner_id=owner_id, kind=artifact.kind)
            )
        except (BadRequest, OSError, RuntimeError, TypeError, ValueError) as exc:
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
            len(item.path.parts),
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


def _prefixed_files(root: Path, owner_id: str) -> tuple[tuple[Path, ...], tuple[str, ...]]:
    try:
        children = tuple(root.iterdir())
    except FileNotFoundError:
        return (), ()
    except OSError as exc:
        message = f"{root!s}: cannot inspect participant artifacts: {exc}"
        logger.warning(message)
        return (), (message,)
    return (
        tuple(
            child
            for child in children
            if _is_regular_or_symlink(child) and child.name.startswith(f"{owner_id}.")
        ),
        (),
    )


def _children(root: Path) -> tuple[Path, ...]:
    try:
        return tuple(root.iterdir())
    except FileNotFoundError:
        return ()
    except OSError as exc:
        logger.warning("cannot inspect participant artifact root %s: %s", root, exc)
        return ()


def _is_regular_or_symlink(path: Path) -> bool:
    try:
        mode = path.lstat().st_mode
    except OSError:
        return False
    return stat.S_ISREG(mode) or stat.S_ISLNK(mode)


def _contains_symlink(path: Path, home: Path) -> bool:
    try:
        relative = path.relative_to(home)
    except ValueError:
        return False
    current = home
    for part in relative.parts:
        current /= part
        try:
            if current.is_symlink():
                return True
        except OSError:
            return True
    return False


def _is_direct_child(path: Path, root: Path) -> bool:
    try:
        return len(path.relative_to(root).parts) == 1
    except ValueError:
        return False


def _is_owned_direct_file(path: Path, root: Path, owner_id: str) -> bool:
    return _is_direct_child(path, root) and path.name.startswith(f"{owner_id}.")


def _is_descendant(path: Path, root: Path, owner_id: str | None) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    if not relative.parts:
        return False
    return owner_id is None or relative.parts[0] == owner_id


def _is_observation_root(path: Path, root: Path, owner_id: str) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    return len(relative.parts) == 2 and relative.parts[1] == owner_id


def _is_observation_descendant(
    path: Path,
    root: Path,
    owner_id: str,
    harness: str | None = None,
) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    return (
        len(relative.parts) >= 3
        and relative.parts[1] == owner_id
        and (harness is None or relative.parts[0] == harness)
    )


def _safe_component(value: str) -> bool:
    return bool(value) and value not in {".", ".."} and Path(value).name == value


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
