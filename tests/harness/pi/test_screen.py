"""Pi screen classification stays conservative and never raises."""

from __future__ import annotations

from theater.harness.builtin.plugins.pi.screen import classify_screen
from theater.harness.contracts.callbacks import ScreenContext
from theater.harness.contracts.observation import ScreenConfidence, ScreenKind


def test_pi_screen_classifier_is_conservative_and_never_raises() -> None:
    assert classify_screen(ScreenContext(capture="Esc to interrupt")).kind is ScreenKind.UNKNOWN
    assert (
        classify_screen(
            ScreenContext(
                capture=(
                    "escape interrupt · ctrl+c/ctrl+d clear/exit\n"
                    "0.0%/1.0M (auto) (mistral) zai-glm-5-2 • high"
                )
            )
        ).kind
        is ScreenKind.UNKNOWN
    )
    idle = classify_screen(ScreenContext(capture="0.0%/1.0M\nTheater: idle"))
    assert idle.kind is ScreenKind.PROMPT
    assert idle.confidence is ScreenConfidence.HIGH
    # A stale idle marker must never beat a real status indicator.
    assert (
        classify_screen(ScreenContext(capture="⠋ Working...\n0.0%/1.0M\nTheater: idle")).kind
        is ScreenKind.WORKING
    )
    # Extension widgets can displace Pi's loader far above the footer.
    assert (
        classify_screen(
            ScreenContext(capture="⠙ Retrying (1/3)\n" + "widget\n" * 12 + "footer")
        ).kind
        is ScreenKind.WORKING
    )
    # Only the final footer-status line can carry the idle marker.
    assert (
        classify_screen(ScreenContext(capture="Theater: idle\nassistant prose")).kind
        is ScreenKind.UNKNOWN
    )
    prompt = classify_screen(ScreenContext(capture="\n❯"))
    assert prompt.kind is ScreenKind.PROMPT
    assert prompt.confidence is ScreenConfidence.LOW
    assert classify_screen(ScreenContext(capture="agent prose only")).kind is ScreenKind.UNKNOWN
    # The awaiting-input marker beats a spinner frame: a pending question is
    # a button-press pane, the same unrecoverable cost as an approval dialog,
    # so the footer wins over a stale loader frame.
    awaiting = classify_screen(
        ScreenContext(capture="⠙ Working...\n0.0%/1.0M\nTheater: awaiting input")
    )
    assert awaiting.kind is ScreenKind.APPROVAL
    assert awaiting.confidence is ScreenConfidence.HIGH
    # Only the final footer-status line can carry the awaiting marker.
    assert (
        classify_screen(ScreenContext(capture="Theater: awaiting input\nassistant prose")).kind
        is ScreenKind.UNKNOWN
    )
    assert (
        classify_screen(ScreenContext(capture="Theater: awaiting input")).kind
        is ScreenKind.APPROVAL
    )
    # An open overlay — a question dialog, a selector — covers the status
    # footer entirely, so the cancel affordance in the final chrome line is
    # the visible awaiting marker.
    overlay = classify_screen(
        ScreenContext(
            capture=" Proceed with the test?\n"
            "❯ 1. Yes\n"
            " Enter to select · ↑/↓ to navigate · Esc to cancel"
        )
    )
    assert overlay.kind is ScreenKind.APPROVAL
    assert overlay.confidence is ScreenConfidence.HIGH
    # The affordance only counts as the final chrome line: prose that
    # mentions it mid-screen cannot spoof an awaiting reading.
    assert (
        classify_screen(
            ScreenContext(capture="assistant mentions Esc to cancel\n0.0%/1.0M\nTheater: idle")
        ).kind
        is ScreenKind.PROMPT
    )
