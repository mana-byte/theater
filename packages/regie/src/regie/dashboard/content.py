"""Small local dashboard copy; availability facts still come from the public catalog."""

from __future__ import annotations

DEFAULT_SENTENCES = (
    "A local view of durable orchestration state.",
    "Names decorate stable participant identities.",
    "Accepted work remains durable after the window closes.",
)


def dashboard_sentence(sentences: list[str] | None, index: int) -> str:
    choices = tuple(sentences) if sentences else DEFAULT_SENTENCES
    return choices[index % len(choices)] if choices else "Régie"


__all__ = ["DEFAULT_SENTENCES", "dashboard_sentence"]
