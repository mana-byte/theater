"""Small generic adapters reusable by manifest authors."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from theater.harness.contracts.callbacks import ScreenClassifier, ScreenContext
from theater.harness.contracts.observation import ScreenConfidence, ScreenKind, ScreenReading

_DEFAULT_SCREEN_READING = ScreenReading(ScreenKind.UNKNOWN, ScreenConfidence.LOW)


@dataclass(frozen=True, slots=True)
class ScreenMarker:
    """One ordered text marker and the reading it proves."""

    text: str
    reading: ScreenReading


def screen_classifier_from_markers(
    markers: Sequence[ScreenMarker],
    *,
    default: ScreenReading = _DEFAULT_SCREEN_READING,
) -> ScreenClassifier:
    """Build a deterministic first-match classifier from generic screen markers."""
    rules = tuple(markers)
    for marker in rules:
        if not isinstance(marker, ScreenMarker):
            raise TypeError("screen markers must contain ScreenMarker values")
        if not marker.text:
            raise ValueError("screen marker text must not be empty")

    def classify(context: ScreenContext) -> ScreenReading:
        for marker in rules:
            if marker.text in context.capture:
                return marker.reading
        return default

    return classify


__all__ = ["ScreenMarker", "screen_classifier_from_markers"]
