"""Status snapshots and message events for the OpenCode TUI frontend tests."""

from __future__ import annotations

from theater.harness.contracts.runtime import (
    RuntimeNotification,
)


def status_snapshot(
    *,
    status: str = "idle",
    session_id: str = "ses-1",
    epoch: int = 1,
) -> RuntimeNotification:
    return RuntimeNotification(
        method="snapshot",
        params={
            "session_id": session_id,
            "route_session_id": session_id,
            "session_epoch": epoch,
            "status": {"type": status},
        },
    )


def message_event(
    info: dict[str, object],
    *,
    session_id: str = "ses-1",
    epoch: int = 1,
) -> RuntimeNotification:
    return RuntimeNotification(
        method="event",
        params={
            "session_id": session_id,
            "route_session_id": session_id,
            "session_epoch": epoch,
            "event": {
                "id": "event-message",
                "type": "message.updated",
                "properties": {"sessionID": session_id, "info": info},
            },
        },
    )
