"""Shared wire projection and exact cached reads for native input requests."""

from __future__ import annotations

from theater.harness.contracts.runtime import NativeHumanInteraction


def interaction_to_wire(interaction: NativeHumanInteraction | None) -> dict[str, object] | None:
    """Serialize the bounded details owned by the native UI."""
    if interaction is None:
        return None
    entry: dict[str, object] = {"kind": str(interaction.kind)}
    if interaction.native_turn_id is not None:
        entry["native_turn_id"] = interaction.native_turn_id
    if interaction.details:
        entry["details"] = interaction.details
    return entry


def cached_pending_interaction(daemon, participant_id: str) -> dict[str, object] | None:
    """Read an exact cached interaction without extending the await deadline."""
    binding = daemon.store.get_runtime_binding(participant_id)
    if binding is None:
        return None
    facts = daemon.runtime_manager.cached_native_facts(
        participant_id,
        backend_generation=binding.backend_generation,
        native_session_id=binding.native_session_id,
    )
    return None if facts is None else interaction_to_wire(facts.pending_interaction)
