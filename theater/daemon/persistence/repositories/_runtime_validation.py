"""Private validation helpers shared by the runtime repositories.

Persistence rejects malformed or oversized values instead of truncating
identity, evidence, or bounds-bearing fields. These helpers mirror the public
contract validators (the harness contract module keeps its own private
copies); the repositories must not import private helpers from
``theater.harness.contracts``.
"""

from __future__ import annotations

from math import isfinite

from theater.constants.harness import HARNESS_RUNTIME_ID_MAX_CHARS


def bounded_id(value: object, label: str) -> None:
    """Require a bounded, non-blank identifier string."""
    if not isinstance(value, str) or not value.strip() or len(value) > HARNESS_RUNTIME_ID_MAX_CHARS:
        raise ValueError(f"{label} must be a bounded non-blank string")


def optional_bounded_id(value: object, label: str) -> None:
    """Require a bounded identifier string or ``None``."""
    if value is None:
        return
    bounded_id(value, label)


def optional_bounded_text(value: object, label: str, *, limit: int) -> None:
    """Require a bounded non-blank string or ``None`` at the given limit."""
    if value is None:
        return
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{label} must be a bounded non-blank string or null")


def generation(value: object, label: str) -> None:
    """Require a non-negative, non-boolean integer generation."""
    if isinstance(value, bool) or type(value) is not int or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")


def optional_generation(value: object, label: str) -> None:
    """Require a non-negative integer generation or ``None``."""
    if value is None:
        return
    generation(value, label)


def optional_queue_sequence(value: object, label: str) -> None:
    """Require a non-negative integer queue position or ``None``."""
    if value is None:
        return
    if isinstance(value, bool) or type(value) is not int or value < 0:
        raise ValueError(f"{label} must be a non-negative integer or null")


def timestamp(value: object, label: str) -> None:
    """Require a non-negative, finite, non-boolean numeric timestamp."""
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not isfinite(value)
        or value < 0
    ):
        raise ValueError(f"{label} must be a non-negative finite timestamp")
