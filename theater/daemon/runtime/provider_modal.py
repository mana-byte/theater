"""Provider prompt-submission gate: fresh identity-fenced screen evidence
must show a prompt composer. Dialogs refuse with ``AwaitingDecision``;
missing, failed, or ambiguous evidence fails closed with ``Busy`` so the
caller — or the followup queue — can retry once the terminal is ready."""

from __future__ import annotations


def provider_modal_gate(daemon):
    """Compose the gate for one daemon; provider routes only, never native."""
    from theater.constants.observation import SCREEN_CAPTURE_MAX_BYTES

    async def check(participant_id: str) -> None:
        from theater.harness import HARNESSES, normalize
        from theater.harness.observation import ScreenConfidence, ScreenKind
        from theater.models import AwaitingDecision, Busy, StaleTarget

        target = daemon.registry.get(participant_id)
        harness = HARNESSES.get(normalize(target.harness))
        if harness is None:
            raise Busy(
                f"participant {participant_id!r} uses harness {target.harness!r}, which is "
                "not loaded, so its terminal screen cannot be classified; not delivering "
                "text — install or enable the harness plugin, then retry"
            )
        binding_before = daemon.store.terminal_bindings.get(participant_id)
        capture_screen = getattr(daemon.presence, "capture_screen", None)
        if capture_screen is None:
            raise Busy(
                f"the presence source of {participant_id!r} supplies no screen "
                "evidence, so prompt readiness cannot be proven; not delivering text "
                "that could press a dialog answer — retry once the provider reports "
                "the screen"
            )
        try:
            capture = await capture_screen(participant_id, max_bytes=SCREEN_CAPTURE_MAX_BYTES)
            reading = harness.observer.screen_reading(capture) if capture else None
        except Exception as exc:
            raise Busy(
                f"reading the terminal screen of {participant_id!r} failed "
                f"({type(exc).__name__}); not delivering text that could press a dialog "
                "answer — retry once the provider reports the screen again"
            ) from exc
        binding_after = daemon.store.terminal_bindings.get(participant_id)
        if _binding_identity(binding_before) != _binding_identity(binding_after):
            raise StaleTarget(
                f"terminal binding of {participant_id!r} changed while its screen was "
                "captured; the reading belongs to a previous terminal — retry the "
                "control against the current terminal"
            )
        if reading is None:
            raise Busy(
                f"the terminal of {participant_id!r} returned no screen evidence, so "
                "prompt readiness cannot be proven; not delivering text that could "
                "press a dialog answer — retry once the provider supplies the screen"
            )
        if reading.kind in (ScreenKind.APPROVAL, ScreenKind.TRUST):
            raise AwaitingDecision(
                f"the terminal of {participant_id!r} is showing a {reading.kind.value} "
                "dialog; submitting text would press its selected answer — not "
                "delivering; answer or dismiss the dialog in the provider terminal, "
                "then retry"
            )
        if reading.kind is not ScreenKind.PROMPT or (
            reading.confidence is not ScreenConfidence.HIGH
        ):
            kind = reading.kind.value
            confidence = reading.confidence.value
            recovery = (
                "update the harness screen classifier to distinguish a ready composer "
                "from approval/trust dialogs at high confidence; a boolean-only or "
                "low-confidence classifier cannot authorize provider prompt submission"
                if reading.kind is ScreenKind.PROMPT
                else "retry once the terminal shows its prompt"
            )
            raise Busy(
                f"the terminal of {participant_id!r} shows {kind} at {confidence} "
                "confidence, not a ready prompt composer; not delivering text that "
                f"could press a dialog answer — {recovery}"
            )

    return check


def _binding_identity(binding) -> tuple[object, ...] | None:
    """The exact terminal identity a captured screen must still belong to."""
    if binding is None:
        return None
    return (
        binding.provider_id,
        binding.provider_generation,
        binding.terminal_id,
        binding.terminal_incarnation,
        dict(binding.occupant_evidence),
        None if binding.process_facts is None else dict(binding.process_facts),
    )


__all__ = ["provider_modal_gate"]
