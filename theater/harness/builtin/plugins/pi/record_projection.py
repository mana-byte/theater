"""Pure live-record projection: preserve Pi semantics without retaining bulk payloads."""

from __future__ import annotations

import json

from .constants import PI_FIELD_TEXT_BYTES, PI_PROJECTED_RECORD_BYTES, PI_RECORD_TEXT_BYTES

_RECORD_FIELDS = (
    "type",
    "id",
    "timestamp",
    "modelId",
    "model",
    "provider",
    "thinkingLevel",
    "tokensBefore",
    "fromId",
    "customType",
)
_MESSAGE_FIELDS = (
    "role",
    "timestamp",
    "stopReason",
    "model",
    "provider",
    "toolName",
    "toolCallId",
    "isError",
)
_USAGE_FIELDS = ("input", "output", "cacheWrite", "cacheRead", "reasoning")


def _pick(value: dict, fields: tuple[str, ...]) -> dict:
    return {key: value[key] for key in fields if key in value}


def _usage(value: dict) -> dict:
    usage = value.get("usage")
    if not isinstance(usage, dict):
        return {}
    projected = _pick(usage, _USAGE_FIELDS)
    if isinstance(cost := usage.get("cost"), dict):
        projected["cost"] = _pick(cost, ("total",))
    return {"usage": projected}


class _TextBudget:
    def __init__(self) -> None:
        self._remaining = PI_RECORD_TEXT_BYTES

    def preview(self, value: str) -> str:
        raw = value.encode("utf-8", errors="replace")
        limit = min(self._remaining, PI_FIELD_TEXT_BYTES)
        if len(raw) <= limit:
            self._remaining -= len(raw)
            return value
        self._remaining -= limit
        # Reserve space for an explicit omission marker, retaining both ends.
        available = max(0, limit - 64)
        head = raw[: available // 2].decode("utf-8", errors="ignore")
        tail = raw[len(raw) - available // 2 :].decode("utf-8", errors="ignore")
        omitted = len(raw) - len(head.encode()) - len(tail.encode())
        return f"{head}… {omitted} bytes omitted …{tail}"


def _content(value: object, budget: _TextBudget) -> object:
    if isinstance(value, str):
        return budget.preview(value)
    if not isinstance(value, list):
        return None
    blocks: list[dict] = []
    for block in value:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "toolCall":
            projected = _pick(block, ("type", "id", "name"))
            arguments = block.get("arguments")
            if arguments is not None:
                arguments_text = (
                    arguments
                    if isinstance(arguments, str)
                    else json.dumps(arguments, sort_keys=True)
                )
                projected["arguments"] = budget.preview(arguments_text)
            blocks.append(projected)
        elif kind == "thinking":
            if isinstance(text := block.get("thinking"), str) and text:
                blocks.append({"type": kind, "thinking": budget.preview(text)})
        elif isinstance(text := block.get("text"), str) and text:
            blocks.append({"type": "text", "text": budget.preview(text)})
        elif kind == "image":
            blocks.append(_pick(block, ("type", "mimeType")))
    return blocks


def project_record(raw: bytes) -> bytes | None:
    """Return bounded JSON, empty bytes for malformed input, or None for excess structure."""
    try:
        record = json.loads(raw)
        if not isinstance(record, dict):
            return b""
        budget = _TextBudget()
        projected = _pick(record, _RECORD_FIELDS) | _usage(record)
        if isinstance(summary := record.get("summary"), str):
            projected["summary"] = budget.preview(summary)
        if isinstance(data := record.get("data"), dict):
            projected["data"] = _pick(data, ("version", "phase"))
        if isinstance(message := record.get("message"), dict):
            output = _pick(message, _MESSAGE_FIELDS) | _usage(message)
            output["content"] = _content(message.get("content"), budget)
            if isinstance(error := message.get("errorMessage"), str):
                output["errorMessage"] = budget.preview(error)
            projected["message"] = output
        encoded = json.dumps(projected, ensure_ascii=False).encode("utf-8")
    except (ValueError, UnicodeError, RecursionError):
        return b""
    return encoded if len(encoded) <= PI_PROJECTED_RECORD_BYTES else None
