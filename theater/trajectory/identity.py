"""Pure canonical trajectory identity helpers."""

from __future__ import annotations

from hashlib import sha256

from theater.constants.trajectory import TRAJECTORY_IDENTIFIER_MAX_BYTES


def bounded_identity(value: str, prefix: str) -> str:
    """Return an identity directly or a deterministic bounded replacement."""
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("trajectory identity must contain valid UTF-8") from exc
    if any(ord(char) < 0x20 or 0x7F <= ord(char) <= 0x9F for char in value):
        raise ValueError("trajectory identity must not contain control characters")
    if len(encoded) <= TRAJECTORY_IDENTIFIER_MAX_BYTES:
        return value
    return f"{prefix}:{sha256(encoded).hexdigest()}"


def fallback_record_id(
    source_epoch: str,
    raw_index: int,
    event_ordinal: int,
    *,
    source_offset: int | None = None,
) -> str:
    """Build the stable baseline identity from trusted source-local coordinates."""
    if not isinstance(source_epoch, str) or not source_epoch:
        raise ValueError("source_epoch must be non-empty")
    if type(raw_index) is not int or raw_index < 0:
        raise ValueError("raw_index must be a non-negative integer")
    if type(event_ordinal) is not int or event_ordinal < 0:
        raise ValueError("event_ordinal must be a non-negative integer")
    if source_offset is not None and (type(source_offset) is not int or source_offset < 0):
        raise ValueError("source_offset must be a non-negative integer or null")
    coordinate = raw_index if source_offset is None else source_offset
    return bounded_identity(f"{source_epoch}:{coordinate}:{event_ordinal}", "fallback")


def namespaced_native_id(native_id: str, source_epoch: str) -> str:
    """Namespace a source-local native identity by its source epoch."""
    return bounded_identity(f"{source_epoch}:{native_id}", "native")


__all__ = ["bounded_identity", "fallback_record_id", "namespaced_native_id"]
