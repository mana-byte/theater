"""Required physical and policy callbacks injected into the control service."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

__all__ = ["ControlGates"]


@dataclass(frozen=True, slots=True)
class ControlGates:
    """Every callback is required; refusals use the existing Theater wire errors."""

    #: Direct-parent/local-operator authorization for the named action;
    #: queue_dispatch revalidates the original caller, not the dispatcher.
    authorize: Callable[[str, str, str], None]

    #: ``(participant_id) -> None``. Refreshes shared focus facts and
    #: refuses protected or unknown targets before any mutation side effect.
    require_absent: Callable[[str], Awaitable[None]]

    #: ``(participant_id) -> None``. Pane/approval/transcript preflights shared
    #: by native and legacy sends; copy mode is deliberately absent here.
    send_preflight: Callable[[str], Awaitable[None]]

    #: ``(participant_id) -> None``. Copy mode blocks legacy key injection
    #: with a transient, actionable refusal; it never gates native delivery.
    legacy_copy_mode_check: Callable[[str], Awaitable[None]]

    #: Legacy-only working-status and send-claim expiry handling.
    #: The composed callback must not suspend between claim handling and return.
    legacy_busy_check: Callable[[str], Awaitable[None]]

    #: ``(prompt) -> None``. Prompt-size and response-format policy, reused
    #: unchanged by ordinary send and queued followups.
    check_prompt: Callable[[str], None]

    #: Model/reasoning policy; approval and sandbox settings are immutable
    #: and deliberately have no control-service gate.
    check_settings: Callable[[str | None, str | None], None]

    #: ``(participant_id) -> str | None``. The participant's working
    #: directory, used for path-touch attribution once a job is dispatched.
    cwd_for: Callable[[str], str | None]

    #: Legacy tmux text delivery; an exception means nothing was delivered,
    #: matching the existing send contract.
    legacy_deliver: Callable[[str, str], Awaitable[None]]
