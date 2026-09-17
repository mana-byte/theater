"""Bounded canonical JSON storage for RC10 persistence records."""

from __future__ import annotations

import json

MAX_PERSISTED_JSON_BYTES = 16 * 1024 * 1024


def encode_json(value: object) -> str:
    encoded = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    if len(encoded.encode("utf-8")) > MAX_PERSISTED_JSON_BYTES:
        raise ValueError("persisted JSON exceeds the 16 MiB storage limit")
    return encoded


def decode_json(value: str) -> object:
    return json.loads(value)


__all__ = ["MAX_PERSISTED_JSON_BYTES", "decode_json", "encode_json"]
