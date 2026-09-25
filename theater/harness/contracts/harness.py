"""The Harness ABC: how to start a harness so it comes up knowing its id.

The MCP SDK passes stdio servers only an env allowlist (mcp/client/stdio/__init__.py:28-44), so
THEATER_ID must be baked into argv: ``theater mcp --id <id>``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from theater.constants.harness import HARNESS_APPROVAL_POLICIES
from theater.harness.contracts.launch import LaunchPlan, ResumeLaunchOverlay
from theater.models import BadRequest

if TYPE_CHECKING:
    from theater.harness.contracts.manifest import ControlManifest, NativeCompatibilityManifest
    from theater.harness.contracts.observation import HarnessObserver
    from theater.harness.contracts.runtime import RuntimeManifest
    from theater.mcp_plugins import McpServerSpec
    from theater.models import Participant

#: No default anywhere — the whole safety story for a child nobody is watching.
APPROVALS = HARNESS_APPROVAL_POLICIES

ResumeStrategy = Literal["continue", "fork"]


@dataclass(frozen=True, slots=True)
class LaunchParameterSupport:
    """Named optional launch parameters an adapter can honour."""

    model: bool = False
    reasoning_effort: bool = False
    resume: bool = False
    approvals: tuple[str, ...] = ()


class Harness(ABC):
    #: Key used on the wire and in `theater spawn <name>`.
    name: str
    #: Executable to look for on PATH.
    binary: str
    #: Extra binary basenames; primary ``binary`` always included; per AGENTS.md plugin-owned.
    binaries: frozenset[str] = frozenset()
    #: Single glyph from a default font; width 1 so no listing reflows.
    icon: str = "·"
    #: Other spellings that resolve to `name` at registration.
    aliases: tuple[str, ...] = ()
    #: Set in __init__; an annotation, not abstract — manifest validation rejects omissions.
    observer: HarnessObserver
    #: Whether the native resume command can also receive a prompt.
    resume_takes_prompt: bool = True
    #: Native resume behaviour. Forking keeps context while minting a new transcript identity.
    resume_strategy: ResumeStrategy = "continue"
    #: Explicit optional launch support compiled from the manifest.
    launch_parameter_support: LaunchParameterSupport = LaunchParameterSupport()
    #: Explicit native controls compiled from the manifest.
    controls: ControlManifest | None = None
    #: Whether this harness explicitly renders generic MCP server specs.
    supports_mcp_rendering: bool = False
    #: Optional native runtime wiring compiled from the manifest; ``None``
    #: means legacy behavior everywhere. An annotation, not abstract: existing
    #: harnesses and sources gain no mandatory methods.
    runtime: RuntimeManifest | None = None
    native_compatibility: NativeCompatibilityManifest | None = None

    # ---- launching ------------------------------------------------------

    @abstractmethod
    def plan_launch(
        self,
        *,
        participant_id: str,
        prompt: str,
        config_path: Path,
        approval: str,
        model: str | None = None,
        mcp_servers: tuple[McpServerSpec, ...] = (),
    ) -> LaunchPlan:
        """Describe how to start this harness. Pure: writes nothing itself.

        `model`/`reasoning_effort` are opaque pass-throughs; adapters unable to select one omit it.
        `mcp_servers` is forwarded only to adapters that accept it.
        """

    def overlay_mcp(
        self,
        plan: LaunchPlan,
        *,
        participant_id: str,
        config_path: Path,
        mcp_servers: tuple[McpServerSpec, ...] = (),
    ) -> LaunchPlan:
        """Render generic MCP server specs onto an existing pure plan (e.g. a runtime backend plan).

        Default returns the plan unchanged: nothing to render, or already rendered in
        ``plan_launch``.
        """
        del participant_id, config_path, mcp_servers
        return plan

    def resume_launch_overlay(
        self,
        *,
        predecessor: Participant,
        trusted_session_owners: Sequence[Participant],
    ) -> ResumeLaunchOverlay:
        """Harness-specific overrides to apply when resuming a predecessor session.

        Refuses a predecessor with a domain: a None overlay domain would silently drop it.
        ``trusted_session_owners`` includes the predecessor itself (markers may name it).
        """
        if predecessor.transcript_domain is None:
            return ResumeLaunchOverlay()
        raise BadRequest(
            f"harness {self.name!r} does not implement resume_launch_overlay "
            "but the predecessor has a transcript domain. The plugin must "
            "implement the hook to resume a session that has a transcript "
            "domain, or the session must be resumed outside Theater and then "
            "adopted and bound."
        )

    def resume_preflight(self, *, predecessor: Participant) -> None:
        """Reject a harness-specific unsafe resume before reservation."""
        del predecessor

    def discover_models(self) -> list[str]:
        """Model names this CLI reports it can run, for `theater models`; a suggestion, never a
        gate.

        Raise `NotImplementedError` if the CLI cannot be asked; [] means asked and told none.
        """
        raise NotImplementedError(
            f"{self.name} cannot list its models: no command or config to read"
        )
