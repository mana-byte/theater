"""The Harness ABC: how to start a harness so it comes up knowing its id.

Two jobs live behind a harness adapter, and they are two objects:

  launching    how to start the harness so it comes up already knowing its
               participant id — ``Harness``, here;
  observing     how to find what it wrote and turn it into harness-independent
               Events — ``HarnessObserver``, in harness/observation.py.

The identity problem, precisely
-------------------------------
A spawned agent must be able to tell the daemon "I am participant X" from
inside its own MCP tool calls. The obvious channel — put THEATER_ID in the
pane's environment and let it be inherited — does not work. The MCP Python SDK
does not pass the parent environment through to a stdio server: when a server
config omits ``env``, the SDK substitutes ``get_default_environment()``, an
allowlist of six variables (HOME, LOGNAME, PATH, SHELL, TERM, USER on posix).
See mcp/client/stdio/__init__.py:28-44,127. Anything else is dropped.

So the id has to be baked into the MCP server's *argv*, which nothing filters:

    theater mcp --id <participant-id>

Each harness needs a different lever to get that argv in place; see the
subclasses. Both harnesses take the initial prompt as a positional argument and
stay interactive, which is what makes phase 5a possible without any keystroke
injection at all.
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
    from theater.harness.contracts.manifest import ControlManifest
    from theater.harness.contracts.observation import HarnessObserver
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

        `model` is opaque and optional. Theater never validates it: vendor
        model namespaces change faster than any allowlist here could, so the
        string is passed through to whatever lever the CLI offers — a flag for
        most, an environment variable for one — and the harness decides. A
        plugin that cannot select a model simply omits the parameter, and
        `harness.plan_launch` rejects the request before reaching it rather
        than dropping the caller's choice on the floor. That omission is the
        compatibility story: an adapter written before this parameter existed
        keeps working for every launch that does not name a model.

        `mcp_servers` is an optional generic rendering input. Older adapters
        may omit it; the public launch funnel forwards it only when accepted.

        `reasoning_effort` follows the same pattern as `model` but is not in
        this abstract signature — it is added per-adapter, and the funnel
        forwards it only to adapters whose `plan_launch` accepts it. A plugin
        that cannot select a reasoning effort simply omits the parameter.
        """

    def resume_launch_overlay(
        self,
        *,
        predecessor: Participant,
        trusted_session_owners: Sequence[Participant],
    ) -> ResumeLaunchOverlay:
        """Harness-specific overrides to apply when resuming a predecessor session.

        Core calls this only when ``req.resume`` is set, after
        ``_validate_resume_identity`` has selected the trusted dead predecessor
        and pre-filtered the trusted matching set. The hook decides whether
        the predecessor's transcript namespace is safe for the successor to
        reuse, and returns the env / domain overrides core merges into the
        launch plan.

        The base implementation is **conditionally fail-closed**:

        - ``predecessor.transcript_domain is None`` → return an empty overlay.
          This is the normal case for a harness with no isolated namespace;
          forcing every such harness to write a boilerplate override would
          be noise.
        - ``predecessor.transcript_domain is not None`` → refuse, naming the
          harness and saying the plugin must implement the hook to resume a
          session that has a transcript domain, or the session must be
          resumed outside Theater and then adopted and bound.

        A universally permissive base is unsafe because
        ``transcript_domain=None`` in the overlay means *no override* and
        preserves the *plan's* domain, not the predecessor's. A resume plan
        commonly returns no domain, so a permissive base would let a declared
        predecessor domain silently disappear.

        ``trusted_session_owners`` is the complete trusted matching set
        (same canonical harness, same requested native session id, trusted
        provenance), **including** the selected predecessor itself. An isolation
        marker may name that row, so excluding it would break the lineage check.
        These owners are normally dead:
        ``_validate_resume_identity`` refuses live trusted matches before
        the hook runs.
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
        """Model names this CLI reports it can run, for `theater models`.

        Optional, and concrete rather than abstract so that not implementing it
        costs a plugin nothing. Guessing for a CLI that exposes no listing would
        produce a catalogue that goes stale silently.

        This is an authoring aid, never a gate. What a spawn may use is the
        `[models]` allowlist in Theater's own config, which the user writes; the
        job here is only to save them typing it. Whatever is returned is
        therefore a suggestion — it may be out of date, may list models the
        user is not authenticated for, and is not consulted at spawn time.

        Raise `NotImplementedError` when the CLI has no way to be asked. Return
        an empty list only for the genuinely different case of having asked and
        been told none, which is what an unauthenticated provider looks like:
        the caller reports those two states differently.
        """
        raise NotImplementedError(
            f"{self.name} cannot list its models: no command or config to read"
        )
