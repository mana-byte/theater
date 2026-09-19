"""Required physical and policy callbacks injected into the control service."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass

from theater.models import Participant

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

    #: Cached fail-closed recheck; never suspend after other awaited preparation.
    check_absent: Callable[[str], None]

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

    #: Live callback health for one exact provider generation.
    provider_health: Callable[[str, int], str] = lambda _provider, _generation: "offline"

    #: Schema-validated duplex callback dispatch. The caller persists intent first.
    provider_dispatch: (
        Callable[[str, int, str, Mapping[str, object]], Awaitable[Mapping[str, object]]] | None
    ) = None

    #: Cache a successfully read native snapshot behind exact runtime identity.
    record_native_snapshot: Callable[[str, object, object], None] = lambda *_args: None

    #: Cached projection of the same transcript trust gate used before sends.
    project_send_preflight: Callable[[Participant], tuple[str | None, str | None]] = (
        lambda _participant: (None, None)
    )

    #: Configured values that make each runtime-supported setting field actionable.
    settings_allowlists: Callable[[str], tuple[Sequence[str], Sequence[str]] | None] = (
        lambda _harness: None
    )
