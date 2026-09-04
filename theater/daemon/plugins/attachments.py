"""Per-participant MCP sidecar planning, confinement, and launch-plan merge."""

from __future__ import annotations

import logging
import stat
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path, PureWindowsPath
from types import MappingProxyType

from theater import paths
from theater.constants.daemon import BUS_KIND_MCP_PLUGIN_OMITTED
from theater.constants.harness import HARNESS_MCP_SERVER_NAME, HARNESS_MCP_WAIT_SERVER_NAME
from theater.constants.plugins import (
    MCP_PLUGIN_API_VERSION,
    MCP_PLUGIN_CREDENTIAL_PATH_ENV,
    MCP_PLUGIN_SPAWN_OMISSION_MAX,
)
from theater.daemon.plugins.credentials import CredentialMaterial, mint_credential
from theater.harness.base import LaunchPlan
from theater.mcp_plugins.contracts import (
    CompiledMcpPlugin,
    McpLaunchPlan,
    McpServerSpec,
    SecretValue,
)
from theater.mcp_plugins.registry import catalog
from theater.models import Participant

logger = logging.getLogger("theater.mcp_plugins.runtime")

_CREDENTIAL_FILENAME = ".theater-plugin-credential"
_RESERVED_SERVER_NAMES = frozenset({HARNESS_MCP_SERVER_NAME, HARNESS_MCP_WAIT_SERVER_NAME})


@dataclass(frozen=True, slots=True)
class PlannedMcpSidecar:
    """A validated launch-local sidecar whose secret is still memory-only."""

    plugin: CompiledMcpPlugin = field(repr=False)
    participant_id: str
    spec: McpServerSpec
    root: Path
    files: Mapping[Path, str]
    private_files: Mapping[Path, str] = field(repr=False)
    credential_path: Path
    credential: CredentialMaterial = field(repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "files", MappingProxyType(dict(self.files)))
        object.__setattr__(self, "private_files", MappingProxyType(dict(self.private_files)))


def plan_sidecars(
    participant: Participant,
    *,
    cwd: str,
    store,
) -> tuple[PlannedMcpSidecar, ...]:
    """Plan and persist every usable configured sidecar without failing a spawn."""
    planned: list[PlannedMcpSidecar] = []
    for plugin in catalog().servers:
        root = paths.participant_mcp_plugin_dir(participant.id, plugin.name)
        if plugin.name in _RESERVED_SERVER_NAMES:
            _rollback_failed_sidecar(store, participant, plugin.name, root)
            _omit(
                store,
                participant,
                plugin,
                stage="planning",
                exc=ValueError(f"MCP server name {plugin.name!r} is reserved by Theater"),
            )
            continue
        try:
            sidecar = _plan_sidecar(plugin, participant_id=participant.id, cwd=cwd)
            _prepare_artifact_root(sidecar)
            store.set_mcp_plugin_credential(
                participant.id,
                plugin_name=plugin.name,
                api_version=MCP_PLUGIN_API_VERSION,
                credential_id=sidecar.credential.credential_id,
                credential_verifier=sidecar.credential.verifier,
                grants=plugin.capabilities,
                credential_path=str(sidecar.credential_path),
            )
        except Exception as exc:
            _rollback_failed_sidecar(store, participant, plugin.name, root)
            _omit(store, participant, plugin, stage="planning", exc=exc)
            continue
        planned.append(sidecar)
    return tuple(planned)


def omit_unrenderable_sidecars(participant: Participant, *, store) -> None:
    """Record configured sidecars omitted by a harness without a generic renderer."""
    for plugin in catalog().servers:
        root = paths.participant_mcp_plugin_dir(participant.id, plugin.name)
        _rollback_failed_sidecar(store, participant, plugin.name, root)
        reason = (
            f"MCP server name {plugin.name!r} is reserved by Theater"
            if plugin.name in _RESERVED_SERVER_NAMES
            else "the selected harness does not render generic MCP server specifications"
        )
        _omit(store, participant, plugin, stage="rendering", exc=ValueError(reason))


def emit_registry_diagnostic_omissions(participant: Participant, *, store) -> None:
    """Turn unavailable enabled registry entries into bounded spawn audit events."""
    snapshot = catalog()
    available = {plugin.name for plugin in snapshot.servers}
    emitted: set[str] = set()
    for diagnostic in sorted(snapshot.diagnostics, key=lambda item: (item.name, item.error)):
        if not diagnostic.requested:
            continue
        if diagnostic.name in available or diagnostic.name in emitted:
            continue
        emitted.add(diagnostic.name)
        _record_omission(
            store,
            participant,
            plugin_name=diagnostic.name,
            stage="registry",
            detail=_safe_registry_diagnostic(diagnostic.error),
        )
        if len(emitted) >= MCP_PLUGIN_SPAWN_OMISSION_MAX:
            break


def omit_conflicting_sidecars(
    sidecars: Iterable[PlannedMcpSidecar],
    plan: LaunchPlan,
    *,
    participant: Participant,
    store,
) -> tuple[PlannedMcpSidecar, ...]:
    """Revoke sidecars whose paths collide with the harness launch plan."""
    occupied = [_canonical_path(path) for path in (*plan.files, *plan.private_files)]
    if plan.receipt_token_path is not None:
        occupied.append(_canonical_path(plan.receipt_token_path))
    occupied.extend(_canonical_path(item.token_path) for item in plan.channel_credentials)

    accepted: list[PlannedMcpSidecar] = []
    for sidecar in sidecars:
        own_paths = tuple(
            sorted((*sidecar.files, *sidecar.private_files, sidecar.credential_path), key=str)
        )
        collision = _first_path_collision(own_paths, occupied)
        if collision is not None:
            own, existing = collision
            _rollback_failed_sidecar(store, participant, sidecar.plugin.name, sidecar.root)
            _omit(
                store,
                participant,
                sidecar.plugin,
                stage="materialization",
                exc=ValueError(
                    f"plugin artifact {str(own)!r} collides with launch artifact {str(existing)!r}"
                ),
            )
            continue
        occupied.extend(_canonical_path(path) for path in own_paths)
        accepted.append(sidecar)
    return tuple(accepted)


def merge_sidecars(plan: LaunchPlan, sidecars: Iterable[PlannedMcpSidecar]) -> LaunchPlan:
    """Attach sidecar artifacts to the core launch plan for ordinary safe writes."""
    files = dict(plan.files)
    private_files = dict(plan.private_files)
    for sidecar in sidecars:
        files.update(sidecar.files)
        private_files.update(sidecar.private_files)
        private_files[sidecar.credential_path] = sidecar.credential.credential + "\n"
    return replace(plan, files=files, private_files=private_files)


def sidecar_specs(sidecars: Iterable[PlannedMcpSidecar]) -> tuple[McpServerSpec, ...]:
    """Return the renderer-ready endpoints in deterministic registry order."""
    return tuple(sidecar.spec for sidecar in sidecars)


def revoke_sidecars(
    sidecars: Iterable[PlannedMcpSidecar],
    *,
    participant: Participant,
    store,
    reason: str,
) -> None:
    """Revoke launch-only records when a sidecar cannot be attached."""
    for sidecar in sidecars:
        _rollback_failed_sidecar(store, participant, sidecar.plugin.name, sidecar.root)
        _omit(store, participant, sidecar.plugin, stage="materialization", exc=ValueError(reason))


def _plan_sidecar(
    plugin: CompiledMcpPlugin,
    *,
    participant_id: str,
    cwd: str,
) -> PlannedMcpSidecar:
    launch = plugin.plan_launch(participant_id=participant_id, cwd=cwd)
    if not isinstance(launch, McpLaunchPlan):
        raise TypeError("MCP launch planner did not return an McpLaunchPlan")
    root = paths.participant_mcp_plugin_dir(participant_id, plugin.name)
    files = _absolute_artifacts(root, launch.files, label="files")
    private_files = _absolute_artifacts(root, launch.private_files, label="private_files")
    credential_path = root / _CREDENTIAL_FILENAME
    artifact_paths = (*files, *private_files)
    if _first_path_collision((credential_path,), artifact_paths) is not None:
        raise ValueError("MCP launch artifact collides with the reserved credential file")
    collision = _first_path_collision(artifact_paths, ())
    if collision is not None:
        first, second = collision
        raise ValueError(
            f"MCP launch artifacts collide after materialization: {str(first)!r}, {str(second)!r}"
        )
    env = dict(launch.env)
    if MCP_PLUGIN_CREDENTIAL_PATH_ENV in env:
        raise ValueError(f"MCP launch env may not set reserved {MCP_PLUGIN_CREDENTIAL_PATH_ENV!r}")
    env[MCP_PLUGIN_CREDENTIAL_PATH_ENV] = str(credential_path)
    return PlannedMcpSidecar(
        plugin=plugin,
        participant_id=participant_id,
        spec=McpServerSpec(
            name=plugin.name,
            command=launch.command,
            args=launch.argv,
            env=env,
        ),
        root=root,
        files=files,
        private_files=private_files,
        credential_path=credential_path,
        credential=mint_credential(),
    )


def _absolute_artifacts(
    root: Path,
    entries: Mapping[Path | str, str],
    *,
    label: str,
) -> dict[Path, str]:
    if not isinstance(entries, Mapping):
        raise TypeError(f"MCP launch {label} must be a mapping")
    result: dict[Path, str] = {}
    for raw_path, content in entries.items():
        relative = _relative_path(raw_path, label)
        if not isinstance(content, str) or "\0" in content:
            raise TypeError(f"MCP launch {label} contents must be text without NUL bytes")
        absolute = root / relative
        if not _canonical_path(absolute).is_relative_to(_canonical_path(root)):
            raise ValueError(f"MCP launch {label} path escapes its artifact root")
        if absolute in result:
            raise ValueError(f"MCP launch {label} repeats artifact path {relative}")
        result[absolute] = content
    return result


def _relative_path(value: object, label: str) -> Path:
    if not isinstance(value, (str, Path)):
        raise TypeError(f"MCP launch {label} paths must be strings or Paths")
    text = str(value)
    path = Path(value)
    windows = PureWindowsPath(text)
    if not text or "\0" in text or path.is_absolute() or windows.is_absolute():
        raise ValueError(f"MCP launch {label} paths must be non-blank relative paths")
    if ".." in path.parts or ".." in windows.parts or str(path) == ".":
        raise ValueError(f"MCP launch {label} paths must not traverse parent directories")
    return path


def _prepare_artifact_root(sidecar: PlannedMcpSidecar) -> None:
    paths.ensure_home()
    _ensure_directory_chain(sidecar.root)
    targets = (*sidecar.files, *sidecar.private_files, sidecar.credential_path)
    for target in targets:
        _ensure_relative_parent(sidecar.root, target.parent)
        if target.exists() or target.is_symlink():
            raise ValueError(f"MCP plugin artifact path already exists: {target}")


def _ensure_relative_parent(root: Path, parent: Path) -> None:
    relative = parent.relative_to(root)
    current = root
    for component in relative.parts:
        current /= component
        _ensure_directory(current)


def _ensure_directory_chain(root: Path) -> None:
    """Create the owned root one real directory at a time."""
    home = paths.home()
    try:
        relative = root.relative_to(home)
    except ValueError as exc:
        raise ValueError(f"MCP plugin artifact root is outside Theater home: {root}") from exc
    _ensure_directory(home, private=False)
    current = home
    for index, component in enumerate(relative.parts):
        current /= component
        _ensure_directory(current, private=index > 0)


def _ensure_directory(path: Path, *, private: bool = True) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        path.mkdir()
        info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ValueError(f"MCP plugin artifact directory is not a real directory: {path}")
    if private:
        path.chmod(0o700)


def _rollback_failed_sidecar(store, participant: Participant, plugin_name: str, root: Path) -> None:
    """Revoke a partially persisted sidecar and remove safe empty directories."""
    try:
        store.delete_mcp_plugin_credential(participant.id, plugin_name)
    except Exception:
        logger.exception(
            "could not revoke failed MCP plugin %s for %s", plugin_name, participant.id
        )
    _remove_empty_root(root)


def _remove_empty_root(root: Path) -> None:
    """Remove only an empty, non-symlink root left by omitted planning."""
    try:
        _remove_empty_tree(root)
    except (FileNotFoundError, OSError):
        return


def _remove_empty_tree(path: Path) -> bool:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        return False
    for child in path.iterdir():
        child_info = child.lstat()
        if stat.S_ISLNK(child_info.st_mode) or not stat.S_ISDIR(child_info.st_mode):
            return False
        if not _remove_empty_tree(child):
            return False
    path.rmdir()
    return True


def _omit(
    store,
    participant: Participant,
    plugin: CompiledMcpPlugin,
    *,
    stage: str,
    exc: Exception,
) -> None:
    detail = _safe_error(plugin, exc)
    _record_omission(
        store,
        participant,
        plugin_name=plugin.name,
        stage=stage,
        detail=detail,
    )


def _record_omission(
    store,
    participant: Participant,
    *,
    plugin_name: str,
    stage: str,
    detail: str,
) -> None:
    logger.warning(
        "omitting MCP plugin %s for %s during %s: %s",
        plugin_name,
        participant.id,
        stage,
        detail,
    )
    try:
        store.bus_append(
            BUS_KIND_MCP_PLUGIN_OMITTED,
            to_id=participant.id,
            payload={"plugin": plugin_name, "stage": stage, "error": detail[:500]},
        )
    except Exception:
        logger.exception("could not record MCP plugin omission for %s", plugin_name)


def _safe_error(plugin: CompiledMcpPlugin, exc: Exception) -> str:
    detail = f"{type(exc).__name__}: {exc}"
    for value in plugin.config.values():
        if isinstance(value, SecretValue):
            detail = detail.replace(value.value, "<redacted>")
    return detail[:500]


def _safe_registry_diagnostic(detail: str) -> str:
    if detail.endswith(" is reserved by Theater"):
        return detail[:500]
    return "enabled plugin was unavailable in the daemon registry"


def _canonical_path(path: Path) -> Path:
    return path.resolve(strict=False)


def _first_path_collision(
    candidates: Iterable[Path], occupied: Iterable[Path]
) -> tuple[Path, Path] | None:
    """Return the first equal or ancestor/descendant artifact collision."""
    prior = tuple(occupied)
    seen: list[Path] = []
    for candidate in candidates:
        canonical = _canonical_path(candidate)
        for existing in (*prior, *seen):
            if _paths_overlap(canonical, existing):
                return canonical, existing
        seen.append(canonical)
    return None


def _paths_overlap(left: Path, right: Path) -> bool:
    return left == right or left.is_relative_to(right) or right.is_relative_to(left)


__all__ = [
    "PlannedMcpSidecar",
    "emit_registry_diagnostic_omissions",
    "merge_sidecars",
    "omit_conflicting_sidecars",
    "omit_unrenderable_sidecars",
    "plan_sidecars",
    "revoke_sidecars",
    "sidecar_specs",
]
