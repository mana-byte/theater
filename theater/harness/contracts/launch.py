"""Launch-plan contracts: what tmux needs to bring a participant up."""

from __future__ import annotations

import shutil
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from theater.harness.contracts.channels import ChannelKind


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
    """

    env: Mapping[str, str] = field(default_factory=dict)
    transcript_domain: str | None = None
    cwd: str | None = None

    def __post_init__(self) -> None:
        if self.cwd is not None and (not isinstance(self.cwd, str) or not self.cwd.strip()):
            raise TypeError("resume launch cwd must be a non-blank string or None")
