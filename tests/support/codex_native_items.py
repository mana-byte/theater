"""Fake codex server pieces and event builders shared by the native runtime tests."""

from __future__ import annotations

from theater.harness.contracts.runtime import (
    RuntimeNotification,
)


def completed_item(
    item_id: str,
    *,
    item_type: str,
    turn_id: str = "turn-1",
    text: str = "answer text",
) -> RuntimeNotification:
    item: dict[str, object] = {"id": item_id, "type": item_type}
    if item_type == "agentMessage":
        item["text"] = text
    elif item_type == "userMessage":
        item["content"] = [{"type": "text", "text": text}]
    return RuntimeNotification(
        method="item/completed",
        params={
            "threadId": "ui-thread-1",
            "turnId": turn_id,
            "completedAtMs": 1234,
            "item": item,
        },
    )
