"""Launch-plan contracts: what tmux needs to bring a participant up."""

from __future__ import annotations

import shutil
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType

from theater.harness.contracts.channels import ChannelKind
from theater.mcp_plugins import McpServerSpec


def theater_binary() -> str:
    """Resolve the absolute path to the ``theater`` executable.

    A spawned tmux window does not inherit the daemon's PATH — tmux starts
    the window from the session's default environment, not the daemon's —
    so the bare name ``"theater"`` would not be found by the harness's MCP
    client when theater was installed via ``uv run`` / a venv. Resolve to
    an absolute path: first check PATH (covers ``uv tool install``), then
    fall back to the bin directory next to ``sys.executable`` (the venv
    case). Returns the bare name as a last resort so the failure is loud
    and diagnosable rather than silent.
    """
    found = shutil.which("theater")
    if found:
        return found
    candidate = Path(sys.executable).parent / "theater"
    if candidate.exists():
        return str(candidate)
    return "theater"


@dataclass(frozen=True, slots=True)
class NativeChild:
    """A sub-agent the harness spawned by itself, outside Theater's knowledge.

    The second lineage edge (§5): Theater did not create them, cannot address
    them, and only learns of them by reading the parent's own bookkeeping.
    """

    session_id: str
    agent: str | None = None
    relative_path: str | None = None
    tool_call_id: str | None = None


@dataclass(frozen=True, slots=True)
class ChannelCredential:
    """Core-owned credential for one bounded native channel."""

    kind: ChannelKind
    channel_id: str
    token: str
    token_path: Path

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ChannelKind):
            raise TypeError("channel credential kind must be a ChannelKind")


@dataclass(frozen=True, slots=True)
class LaunchPlan:
    """Everything tmux needs to bring a participant up."""

    argv: list[str]
    env: dict[str, str] = field(default_factory=dict)
    #: Files to write before the window is created, path -> contents.
    files: dict[Path, str] = field(default_factory=dict)
    #: Files containing launch secrets, written mode 0600 by the daemon.
    private_files: dict[Path, str] = field(default_factory=dict)
    #: Exact native session id; persisted before tmux starts to avoid cwd guessing.
    session_id: str | None = None
    #: Core-populated output, not plugin input; filled by the spawner before any file write.
    receipt_token: str | None = None
    #: Where the daemon writes receipt_token (0600) and deletes on death; core-owned.
    receipt_token_path: Path | None = None
    #: Resolved transcript namespace; persisted before launch so collision policy is stable.
    transcript_domain: str | None = None
    #: Core-populated native channel credentials; plugins never receive token bytes.
    channel_credentials: tuple[ChannelCredential, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "channel_credentials", tuple(self.channel_credentials))


@dataclass(frozen=True, slots=True)
class McpRenderContext:
    """The base plan and participant-scoped stdio servers for one renderer."""

    participant_id: str
    config_path: Path
    plan: LaunchPlan
    servers: tuple[McpServerSpec, ...] | Sequence[McpServerSpec]

    def __post_init__(self) -> None:
        if not isinstance(self.participant_id, str) or not self.participant_id.strip():
            raise TypeError("MCP render participant_id must be a non-blank string")
        if not isinstance(self.config_path, Path):
            raise TypeError("MCP render config_path must be a Path")
        if not isinstance(self.plan, LaunchPlan):
            raise TypeError("MCP render plan must be a LaunchPlan")
        if isinstance(self.servers, (str, bytes)) or not isinstance(self.servers, Sequence):
            raise TypeError("MCP render servers must be a sequence of McpServerSpec values")
        servers = tuple(self.servers)
        if any(not isinstance(server, McpServerSpec) for server in servers):
            raise TypeError("MCP render servers must contain only McpServerSpec values")
        object.__setattr__(self, "servers", servers)


@dataclass(frozen=True, slots=True)
class McpRenderOverlay:
    """The bounded argv, environment, and public-file changes from one renderer."""

    argv: tuple[str, ...] | Sequence[str] = ()
    argv_insert_at: int | None = None
    env: Mapping[str, str] = field(default_factory=dict)
    files: Mapping[Path, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if isinstance(self.argv, (str, bytes)) or not isinstance(self.argv, Sequence):
            raise TypeError("MCP render argv must be a sequence of strings")
        argv = tuple(self.argv)
        if any(not isinstance(value, str) for value in argv):
            raise TypeError("MCP render argv must contain only strings")
        if self.argv_insert_at is not None and (
            type(self.argv_insert_at) is not int or self.argv_insert_at < 0
        ):
            raise TypeError("MCP render argv_insert_at must be a non-negative integer or None")
        if not isinstance(self.env, Mapping) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in self.env.items()
        ):
            raise TypeError("MCP render env must be a mapping of strings")
        if not isinstance(self.files, Mapping) or any(
            not isinstance(path, Path) or not isinstance(contents, str)
            for path, contents in self.files.items()
        ):
            raise TypeError("MCP render files must be a mapping of Path to string")
        object.__setattr__(self, "argv", argv)
        object.__setattr__(self, "env", MappingProxyType(dict(self.env)))
        object.__setattr__(self, "files", MappingProxyType(dict(self.files)))


@dataclass(frozen=True, slots=True)
class ResumeLaunchOverlay:
    """Harness-specific overrides to merge into a launch plan on resume.

    Returned by ``Harness.resume_launch_overlay`` when core resumes a session.
    The fields are the only things a plugin may influence around a resume:

    - ``env``: extra environment variables (or overrides for plan env) that
      the successor process needs. Merged as ``{**plan.env, **overlay.env}``,
      so the overlay wins on conflict.
    - ``transcript_domain``: the namespace to persist on the successor row.
      ``None`` means *no override* — core keeps whatever ``plan_launch``
      returned. It does **not** mean "clear it"; that would let a declared
      predecessor domain silently disappear on a resume plan that returns
      ``transcript_domain=None``.
    - ``cwd``: an authoritative working directory for the successor launch.
      ``None`` keeps the requested cwd. Core applies a non-None value before
      creating the successor participant or planning its launch.
    - ``resume_reference``: an alternate native resume reference handed to
      ``plan_launch``. ``None`` preserves the requested native session id.
      This lets a harness resume by a trusted transcript path when its CLI
      cannot resolve that transcript inside the successor's isolated domain.
    """

    env: Mapping[str, str] = field(default_factory=dict)
    transcript_domain: str | None = None
    cwd: str | None = None
    resume_reference: str | None = None

    def __post_init__(self) -> None:
        if self.cwd is not None and (not isinstance(self.cwd, str) or not self.cwd.strip()):
            raise TypeError("resume launch cwd must be a non-blank string or None")
        if self.resume_reference is not None and (
            not isinstance(self.resume_reference, str) or not self.resume_reference.strip()
        ):
            raise TypeError("resume launch reference must be a non-blank string or None")
