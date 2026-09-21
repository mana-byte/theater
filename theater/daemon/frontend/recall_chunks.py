"""UTF-8-safe recall chunks with logarithmic wire-budget fitting."""

from __future__ import annotations

import json

from theater.daemon.frontend.validation import PublicRequestError


def _encoded_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def recall_chunk(segment_id: str, value: dict, *, offset: int, max_bytes: int) -> dict[str, object]:
    """Run off-loop: the source value is already bounded by the recall reader."""
    encoded = _encoded_json(value)
    if not 0 <= offset <= len(encoded):
        raise PublicRequestError("bad_request", "recall read offset is outside the segment")
    if offset < len(encoded) and encoded[offset] & 0xC0 == 0x80:
        raise PublicRequestError(
            "bad_request",
            "recall read offset splits a UTF-8 character; use the returned next_offset",
        )

    def build(end: int) -> dict[str, object]:
        content = encoded[offset:end].decode("utf-8", errors="ignore")
        boundary = offset + len(content.encode("utf-8"))
        return {
            "segment_id": segment_id,
            "offset": offset,
            "next_offset": boundary if boundary < len(encoded) else None,
            "total_bytes": len(encoded),
            "encoding": "json",
            "content": content,
        }

    low, high = offset, min(len(encoded), offset + max_bytes)
    full = build(high)
    if len(_encoded_json(full)) <= max_bytes:
        return full
    best = None
    while low <= high:
        middle = (low + high) // 2
        candidate = build(middle)
        if len(_encoded_json(candidate)) <= max_bytes:
            best = candidate
            low = middle + 1
        else:
            high = middle - 1
    if best is None or (not best["content"] and offset < len(encoded)):
        raise PublicRequestError(
            "too_large",
            "recall read budget cannot fit paging metadata and text; increase max_bytes",
        )
    return best
