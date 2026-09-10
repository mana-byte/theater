"""Injected seams for the daemon control service.

The control service owns authorization, idle checks, job correlation, the
followup queue, and delivery recovery — but none of the physical facts. Pane
ownership, copy-mode human presence, working-status semantics, prompt-size
limits, model allowlists, and legacy tmux delivery all belong to the daemon
composition that wires this service. Every gate is an explicitly injected
callable; there are no permissive defaults, so a composition that forgets one
fails at construction instead of silently allowing traffic through.

Ordinary send retains its current permissions and preflights through these
seams; steer, queue, settings, and interrupt add the direct-parent / local
operator authorization the runtime-wiring plan requires. The same
authorization gate runs again when a queued followup actually dispatches —
ownership is revalidated at dispatch, not just at queue time.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

__all__ = ["ControlGates"]


@dataclass(frozen=True, slots=True)
class ControlGates:
    """Every physical/policy fact the control service needs, injected.

    All fields are required. A gate raises the existing ``theater.models``
    errors (``NotYourChild``, ``NotAddressable``, ``HumanPresent``,
    ``Busy``, ``AwaitingDecision``, ``BadRequest``, …) — the wire codes the
    clients already branch on.
    """

    #: ``(participant_id, caller_id, action) -> None``. Direct parent /
    #: local-operator authorization. Actions: ``send``, ``steer``,
    #: ``queue_followup``, ``queue_dispatch``, ``settings_update``,
    #: ``interrupt``. ``queue_dispatch`` revalidates the *original* caller's
    #: ownership when a queued followup actually dispatches.
    authorize: Callable[[str, str, str], None]

    #: ``(participant_id) -> None``. Pane ownership, addressability,
    #: copy-mode human presence, approval-modal, and transcript preflight —
    #: the existing ordinary-send gates, shared by native and legacy sends.
    #: Refusals here are delivery policy, never a capability question.
    send_preflight: Callable[[str], Awaitable[None]]

    #: ``(participant_id) -> None``. Legacy (no runtime) busy semantics —
    #: working-status and send-claim expiry handling. Only consulted on the
    #: legacy transport.
    legacy_busy_check: Callable[[str], Awaitable[None]]

    #: ``(prompt) -> None``. Prompt-size and response-format policy, reused
    #: unchanged by ordinary send and queued followups.
    check_prompt: Callable[[str], None]

    #: ``(model, reasoning_effort) -> None``. Model/reasoning allowlists for
    #: settings updates. Approval and sandbox policy are immutable and have
    #: no gate: the service has no field that could set them.
    check_settings: Callable[[str | None, str | None], None]

    #: ``(participant_id) -> str | None``. The participant's working
    #: directory, used for path-touch attribution once a job is dispatched.
    cwd_for: Callable[[str], str | None]

    #: ``(participant_id, prompt) -> None``. Legacy tmux text delivery. An
    #: exception means nothing was delivered, matching the existing send
    #: contract.
    legacy_deliver: Callable[[str, str], Awaitable[None]]
