"""Immutable public contracts for MCP-server plugin packages."""

from __future__ import annotations

import inspect
import re
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from enum import StrEnum
from math import isfinite
from pathlib import Path, PureWindowsPath
from types import MappingProxyType
from typing import Protocol

from theater.constants.core import HARNESS_NAME
from theater.constants.plugins import (
    MCP_PLUGIN_LAUNCH_MAX_ARGV,
    MCP_PLUGIN_LAUNCH_MAX_ARTIFACTS,
    MCP_PLUGIN_LAUNCH_MAX_ENV,
    MCP_PLUGIN_LAUNCH_MAX_TEXT_CHARS,
    MCP_PLUGIN_LAUNCH_MAX_VALUE_CHARS,
    PLUGIN_API_VERSION,
)

MANIFEST_API_VERSION = PLUGIN_API_VERSION
MCP_PLUGIN_API_VERSION = PLUGIN_API_VERSION

_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class PluginCapability(StrEnum):
    """A complete, stable capability granted to one MCP-server sidecar."""

    PARTICIPANTS_READ = "participants.read"
    PARTICIPANTS_METADATA_WRITE = "participants.metadata.write"
    CATALOG_READ = "catalog.read"
    JOBS_READ = "jobs.read"
    JOBS_AWAIT = "jobs.await"
    TRANSCRIPTS_READ = "transcripts.read"
    RECALL_READ = "recall.read"
    SKILLS_READ = "skills.read"
    TRAJECTORY_READ = "trajectory.read"
    ANALYTICS_READ = "analytics.read"
    SCRATCHPAD_READ = "scratchpad.read"
    SCRATCHPAD_WRITE = "scratchpad.write"
    SESSIONS_SPAWN = "sessions.spawn"
    SESSIONS_SEND = "sessions.send"
    SESSIONS_INTERRUPT = "sessions.interrupt"
    SESSIONS_KILL = "sessions.kill"


class McpConfigKind(StrEnum):
    """The value kinds a plugin may declare in its configuration schema."""

    STRING = "string"
    INTEGER = "integer"
    FLOAT = "float"
    BOOLEAN = "boolean"
    PATH = "path"
    STRING_LIST = "list[str]"
    LIST_OF_STRING = "list[str]"
    TABLE_LIST = "list[table]"
    SECRET = "secret"


class _Missing:
    def __repr__(self) -> str:
        return "MISSING"


MISSING = _Missing()


@dataclass(frozen=True, slots=True)
class McpConfigField:
    """One declarative MCP-plugin configuration field."""

    kind: McpConfigKind | str
    required: bool = False
    default: object = MISSING
    minimum: int | float | None = None
    maximum: int | float | None = None
    choices: frozenset[str] | None = None
    min_length: int | None = None
    max_length: int | None = None
    min_items: int | None = None
    max_items: int | None = None
    path_must_exist: bool = False
    path_must_be_absolute: bool = False
    item_schema: McpConfigSchema | None = None

    def __post_init__(self) -> None:
        if isinstance(self.kind, str):
            normalized = {
                "string_list": McpConfigKind.STRING_LIST,
                "list_of_string": McpConfigKind.STRING_LIST,
                "list-of-string": McpConfigKind.STRING_LIST,
            }.get(self.kind)
            if normalized is not None:
                object.__setattr__(self, "kind", normalized)
            else:
                with suppress(ValueError):
                    object.__setattr__(self, "kind", McpConfigKind(self.kind))
        if self.choices is not None and not isinstance(self.choices, (str, bytes)):
            object.__setattr__(self, "choices", frozenset(self.choices))


@dataclass(frozen=True, slots=True)
class McpConfigSchema:
    """A named mapping of configuration fields for one plugin."""

    fields: Mapping[str, McpConfigField] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if isinstance(self.fields, Mapping):
            object.__setattr__(self, "fields", MappingProxyType(dict(self.fields)))


@dataclass(frozen=True, slots=True, repr=False)
class SecretReference:
    """A declarative secret source before it is resolved once at startup."""

    source: str
    target: str

    def __repr__(self) -> str:
        return "SecretReference(<redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class SecretValue:
    """An immutable resolved secret whose display forms never reveal its bytes."""

    value: str = field(repr=False)

    def __repr__(self) -> str:
        return "SecretValue(<redacted>)"

    def __str__(self) -> str:
        return "<redacted>"


@dataclass(frozen=True, slots=True)
class McpLaunchContext:
    """The only participant facts passed to a synchronous sidecar planner."""

    participant_id: str
    cwd: Path | str
    config: Mapping[str, object]

    def __post_init__(self) -> None:
        _require_nonblank_text(self.participant_id, "MCP launch participant_id")
        if isinstance(self.cwd, str) and not self.cwd.strip():
            raise ValueError("MCP launch cwd must not be blank")
        if not isinstance(self.cwd, (str, Path)):
            raise TypeError("MCP launch cwd must be a path")
        object.__setattr__(self, "cwd", Path(self.cwd))
        object.__setattr__(self, "config", _freeze_config(self.config))


@dataclass(frozen=True, slots=True)
class McpLaunchPlan:
    """A bounded stdio launch description with only relative text artifacts."""

    command: str
    argv: tuple[str, ...] | Sequence[str] = ()
    env: Mapping[str, str] = field(default_factory=dict, repr=False)
    files: Mapping[Path | str, str] = field(default_factory=dict)
    private_files: Mapping[Path | str, str] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        _require_nonblank_text(self.command, "MCP launch command")
        if len(self.command) > MCP_PLUGIN_LAUNCH_MAX_VALUE_CHARS or "\0" in self.command:
            raise ValueError("MCP launch command is too long or contains a NUL byte")
        object.__setattr__(self, "argv", _freeze_argv(self.argv))
        object.__setattr__(self, "env", _freeze_env(self.env))
        files = _freeze_artifacts(self.files, "files")
        private_files = _freeze_artifacts(self.private_files, "private_files")
        duplicate = set(files) & set(private_files)
        if duplicate:
            raise ValueError(
                f"MCP launch artifact {sorted(duplicate, key=str)[0]} is both public and private"
            )
        object.__setattr__(self, "files", files)
        object.__setattr__(self, "private_files", private_files)


class McpLaunchPlanner(Protocol):
    """Build one pure launch plan for a participant-scoped MCP sidecar."""

    def __call__(self, context: McpLaunchContext) -> McpLaunchPlan: ...


@dataclass(frozen=True, slots=True)
class McpLaunchManifest:
    """The declarative planner callback for one MCP-server package."""

    planner: McpLaunchPlanner


@dataclass(frozen=True, slots=True)
class McpServerManifest:
    """The complete immutable contract for one canonical MCP-server package."""

    api_version: int
    description: str
    capabilities: frozenset[PluginCapability]
    launch: McpLaunchManifest
    config: McpConfigSchema = field(default_factory=McpConfigSchema)
    skills: tuple[str, ...] | Sequence[str] = ()
    _capabilities_were_frozen: bool = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "_capabilities_were_frozen",
            isinstance(self.capabilities, frozenset),
        )
        if not isinstance(self.capabilities, (str, bytes)):
            object.__setattr__(self, "capabilities", frozenset(self.capabilities))
        if isinstance(self.config, Mapping):
            object.__setattr__(self, "config", McpConfigSchema(self.config))
        if not isinstance(self.skills, (str, bytes)) and isinstance(self.skills, Sequence):
            object.__setattr__(self, "skills", tuple(self.skills))

    @property
    def configuration(self) -> McpConfigSchema:
        """Compatibility spelling for the declarative configuration schema."""
        return self.config


@dataclass(frozen=True, slots=True)
class CompiledMcpPlugin:
    """A canonical MCP plugin with validated configuration ready to plan launches."""

    name: str
    description: str
    capabilities: frozenset[PluginCapability]
    config: Mapping[str, object]
    launch: McpLaunchManifest = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or HARNESS_NAME.fullmatch(self.name) is None:
            raise ValueError("MCP plugin name must be canonical")
        _require_nonblank_text(self.description, "MCP plugin description")
        if not isinstance(self.capabilities, frozenset) or not self.capabilities:
            raise TypeError("MCP plugin capabilities must be a non-empty frozenset")
        if any(not isinstance(capability, PluginCapability) for capability in self.capabilities):
            raise TypeError("MCP plugin capabilities must contain PluginCapability values")
        if not isinstance(self.launch, McpLaunchManifest):
            raise TypeError("MCP plugin launch must be an McpLaunchManifest")
        object.__setattr__(self, "capabilities", frozenset(self.capabilities))
        object.__setattr__(self, "config", _freeze_config(self.config))

    def plan_launch(self, *, participant_id: str, cwd: Path | str) -> McpLaunchPlan:
        """Invoke the manifest's synchronous planner with no core credentials."""
        result = self.launch.planner(
            McpLaunchContext(participant_id=participant_id, cwd=cwd, config=self.config)
        )
        if inspect.isawaitable(result):
            raise TypeError("MCP launch planner must be synchronous")
        if not isinstance(result, McpLaunchPlan):
            raise TypeError("MCP launch planner must return an McpLaunchPlan")
        return result


@dataclass(frozen=True, slots=True)
class McpServerSpec:
    """A renderer-ready stdio endpoint for one participant."""

    name: str
    command: str
    args: tuple[str, ...] | Sequence[str] = ()
    env: Mapping[str, str] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or HARNESS_NAME.fullmatch(self.name) is None:
            raise ValueError("MCP server name must be canonical")
        _require_nonblank_text(self.command, "MCP server command")
        if len(self.command) > MCP_PLUGIN_LAUNCH_MAX_VALUE_CHARS or "\0" in self.command:
            raise ValueError("MCP server command is too long or contains a NUL byte")
        object.__setattr__(self, "args", _freeze_argv(self.args))
        object.__setattr__(self, "env", _freeze_env(self.env))


def _freeze_argv(value: object) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise TypeError("MCP launch argv must be a sequence of strings")
    if len(value) > MCP_PLUGIN_LAUNCH_MAX_ARGV:
        raise ValueError(f"MCP launch argv may contain at most {MCP_PLUGIN_LAUNCH_MAX_ARGV} values")
    argv = tuple(value)
    for item in argv:
        if not isinstance(item, str) or "\0" in item:
            raise TypeError("MCP launch argv must contain strings without NUL bytes")
        if len(item) > MCP_PLUGIN_LAUNCH_MAX_VALUE_CHARS:
            raise ValueError("MCP launch argv value is too long")
    return argv


def _freeze_env(value: object) -> Mapping[str, str]:
    if not isinstance(value, Mapping):
        raise TypeError("MCP launch env must be a mapping of strings")
    if len(value) > MCP_PLUGIN_LAUNCH_MAX_ENV:
        raise ValueError(f"MCP launch env may contain at most {MCP_PLUGIN_LAUNCH_MAX_ENV} values")
    env: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or _ENV_NAME.fullmatch(key) is None:
            raise ValueError("MCP launch env keys must be environment variable names")
        if not isinstance(item, str) or "\0" in item:
            raise TypeError("MCP launch env values must be strings without NUL bytes")
        if len(item) > MCP_PLUGIN_LAUNCH_MAX_VALUE_CHARS:
            raise ValueError("MCP launch env value is too long")
        env[key] = item
    return MappingProxyType(env)


def _freeze_artifacts(value: object, label: str) -> Mapping[Path, str]:
    if not isinstance(value, Mapping):
        raise TypeError(f"MCP launch {label} must be a mapping of relative paths to text")
    if len(value) > MCP_PLUGIN_LAUNCH_MAX_ARTIFACTS:
        raise ValueError(
            f"MCP launch {label} may contain at most {MCP_PLUGIN_LAUNCH_MAX_ARTIFACTS} files"
        )
    artifacts: dict[Path, str] = {}
    total = 0
    for raw_path, text in value.items():
        path = _relative_artifact_path(raw_path, label)
        if path in artifacts:
            raise ValueError(f"MCP launch {label} repeats artifact path {path}")
        if not isinstance(text, str) or "\0" in text:
            raise TypeError(f"MCP launch {label} contents must be text without NUL bytes")
        total += len(text)
        if total > MCP_PLUGIN_LAUNCH_MAX_TEXT_CHARS:
            raise ValueError(
                f"MCP launch {label} exceeds {MCP_PLUGIN_LAUNCH_MAX_TEXT_CHARS} characters"
            )
        artifacts[path] = text
    return MappingProxyType(artifacts)


def _relative_artifact_path(value: object, label: str) -> Path:
    if not isinstance(value, (str, Path)):
        raise TypeError(f"MCP launch {label} paths must be strings or Paths")
    text = str(value)
    if not text or "\0" in text:
        raise ValueError(f"MCP launch {label} paths must be non-blank relative paths")
    path = Path(value)
    windows = PureWindowsPath(text)
    if path.is_absolute() or windows.is_absolute():
        raise ValueError(f"MCP launch {label} paths must be relative")
    if ".." in path.parts or ".." in windows.parts:
        raise ValueError(f"MCP launch {label} paths must not traverse parent directories")
    if str(path) == ".":
        raise ValueError(f"MCP launch {label} paths must name a file")
    return path


def _freeze_config(value: object) -> Mapping[str, object]:
    return _freeze_config_mapping(value, set())


def _freeze_config_mapping(value: object, active: set[int]) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError("MCP launch config must be a mapping")
    identity = id(value)
    if identity in active:
        raise ValueError("MCP launch config must not contain cycles")
    active.add(identity)
    try:
        frozen: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("MCP launch config keys must be strings")
            frozen[key] = _freeze_config_value(item, active)
        return MappingProxyType(frozen)
    finally:
        active.remove(identity)


def _freeze_config_value(value: object, active: set[int]) -> object:
    if value is None or type(value) in (bool, int) or isinstance(value, (str, Path, SecretValue)):
        return value
    if type(value) is float:
        if not isfinite(value):
            raise ValueError("MCP launch config numbers must be finite")
        return value
    if isinstance(value, Mapping):
        return _freeze_config_mapping(value, active)
    if isinstance(value, tuple) and all(isinstance(item, str) for item in value):
        return tuple(value)
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return tuple(value)
    if isinstance(value, tuple) and all(isinstance(item, Mapping) for item in value):
        return tuple(_freeze_config_mapping(item, active) for item in value)
    raise TypeError("MCP launch config values must be validated immutable plugin values")


def _require_nonblank_text(value: object, label: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be a string")
    if not value.strip():
        raise ValueError(f"{label} must not be blank")


ConfigField = McpConfigField
ConfigKind = McpConfigKind
ConfigSchema = McpConfigSchema
LaunchManifest = McpLaunchManifest
LaunchPlan = McpLaunchPlan

__all__ = [
    "MANIFEST_API_VERSION",
    "MCP_PLUGIN_API_VERSION",
    "MISSING",
    "PLUGIN_API_VERSION",
    "CompiledMcpPlugin",
    "ConfigField",
    "ConfigKind",
    "ConfigSchema",
    "LaunchManifest",
    "LaunchPlan",
    "McpConfigField",
    "McpConfigKind",
    "McpConfigSchema",
    "McpLaunchContext",
    "McpLaunchManifest",
    "McpLaunchPlan",
    "McpLaunchPlanner",
    "McpServerManifest",
    "McpServerSpec",
    "PluginCapability",
    "SecretReference",
    "SecretValue",
]
