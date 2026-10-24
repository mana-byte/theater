"""Pure control-report facts shared by private and frontend reads."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from theater.daemon.controls.interaction_wire import interaction_to_wire
from theater.daemon.controls.routing import ControlRoute
from theater.daemon.harness_runtime.manager import CachedNativeRoute
from theater.daemon.persistence.repositories.runtime_bindings import ParticipantRuntimeBinding
from theater.harness.contracts.runtime import (
    ConnectionHealth,
    NativeHumanInteraction,
    RuntimeSettings,
    RuntimeSnapshot,
    RuntimeWiring,
)
from theater.models import TerminalBindingRecord


@dataclass(frozen=True, slots=True)
class ControlActiveTurn:
    native_turn_id: str
    job_handle: str | None
    pending_interaction: NativeHumanInteraction | None


@dataclass(frozen=True, slots=True)
class ControlReport:
    participant_id: str
    wiring: str
    backend_generation: int | None
    native_session_id: str | None
    health: ConnectionHealth | str | None
    diagnostics: tuple[str, ...]
    settings: RuntimeSettings | None
    active_turn: ControlActiveTurn | None
    queued: tuple[str, ...]
    human_presence: Mapping[str, object]
    provider_terminal: TerminalBindingRecord | None


def build_control_report(
    participant_id: str,
    *,
    native: CachedNativeRoute | RuntimeSnapshot | None,
    binding: ParticipantRuntimeBinding | None,
    provider_route: ControlRoute | None,
    active_turn: ControlActiveTurn | None,
    queued: Sequence[str],
    human_presence: Mapping[str, object],
    native_wiring: RuntimeWiring = RuntimeWiring.NATIVE,
) -> ControlReport:
    """Choose the same native, disconnected, provider, or legacy report facts."""
    provider_terminal = None
    settings = None
    if native is not None:
        wiring = str(native_wiring)
        generation = native.backend_generation
        session = native.native_session_id
        health: ConnectionHealth | str | None = native.health
        diagnostics = native.health_diagnostics
        settings = native.settings
    elif binding is not None:
        wiring = str(binding.wiring)
        generation = binding.backend_generation
        session = binding.native_session_id
        health = ConnectionHealth.DISCONNECTED
        diagnostics = ("no runtime connection: the daemon reconnects during recovery",)
    elif provider_route is not None and provider_route.is_provider:
        wiring = "provider"
        generation = None
        session = None
        health = provider_route.provider_health
        diagnostics = ()
        provider_terminal = provider_route.terminal
    else:
        wiring = str(RuntimeWiring.LEGACY)
        generation = None
        session = None
        health = None
        diagnostics = ()
    return ControlReport(
        participant_id,
        wiring,
        generation,
        session,
        health,
        diagnostics,
        settings,
        active_turn if native is not None else None,
        tuple(queued),
        human_presence,
        provider_terminal,
    )


def control_report_to_wire(
    report: ControlReport,
    *,
    capabilities: dict[str, object] | None = None,
    transcript_identity: dict[str, object] | None = None,
) -> dict[str, object]:
    """Serialize at either adapter boundary, preserving its optional fields and order."""
    health = (
        None
        if report.health is None
        else {"connection": str(report.health), "diagnostics": list(report.diagnostics)}
    )
    settings = (
        None
        if report.settings is None
        else {
            "model": report.settings.model,
            "reasoning_effort": report.settings.reasoning_effort,
            "supported_fields": sorted(str(field) for field in report.settings.supported_fields),
        }
    )
    active_turn: dict[str, object] | None = None
    if report.active_turn is not None:
        active_turn = {
            "native_turn_id": report.active_turn.native_turn_id,
            "job_handle": report.active_turn.job_handle,
        }
        interaction = interaction_to_wire(report.active_turn.pending_interaction)
        if interaction is not None:
            active_turn["pending_interaction"] = interaction
    value: dict[str, object] = {
        "id": report.participant_id,
        "wiring": report.wiring,
        "backend_generation": report.backend_generation,
        "native_session_id": report.native_session_id,
        "health": health,
        "settings": settings,
    }
    if capabilities is not None:
        value["capabilities"] = capabilities
    if transcript_identity is not None:
        value["transcript_identity"] = transcript_identity
    value["active_turn"] = active_turn
    value["queued"] = list(report.queued)
    value["human_presence"] = dict(report.human_presence)
    terminal = report.provider_terminal
    if terminal is not None:
        value["provider_id"] = terminal.provider_id
        value["provider_generation"] = terminal.provider_generation
        value["terminal_id"] = terminal.terminal_id
        value["terminal_incarnation"] = terminal.terminal_incarnation
    return value
