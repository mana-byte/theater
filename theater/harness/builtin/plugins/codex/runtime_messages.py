"""Pure payload and text helpers for the Codex native runtime."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from pathlib import Path

from theater.harness.contracts.runtime import NativeTurnTerminal, RuntimeConnectionError
from theater.trajectory.enums import TrajectoryKind, TrajectoryLane, TrajectoryStatus

from .runtime_constants import (
    CODEX_RUNTIME_RECONCILE_PAGE_SIZE,
    CODEX_RUNTIME_RECONCILE_TURNS,
    CODEX_RUNTIME_REVISION_MAX,
)


def _bounded_str(value: object, *, limit: int) -> str | None:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        return None
    return value


def _same_cwd(broadcast: object, want: str) -> bool:
    """Whether a broadcast cwd and the participant cwd name one directory.

    Codex broadcasts the canonical path (``/tmp`` → ``/private/tmp``), so compare resolved paths.
    """
    if not isinstance(broadcast, str) or not broadcast:
        return False
    try:
        return Path(broadcast).resolve() == Path(want).resolve()
    except OSError:
        return broadcast == want


def _thread_id_of(thread: object) -> str | None:
    if not isinstance(thread, Mapping):
        return None
    return _bounded_str(thread.get("id"), limit=512)


def _resume_params(session: str) -> dict[str, object]:
    # initialTurnsPage is a *separate* response field. It does not disable
    # full thread.turns hydration; excludeTurns is essential on stock 0.154.
    return {
        "threadId": session,
        "excludeTurns": True,
        "initialTurnsPage": {
            "limit": CODEX_RUNTIME_RECONCILE_TURNS,
            "itemsView": "summary",
            "sortDirection": "desc",
        },
    }


def _completed_at(turn: Mapping[str, object]) -> float | None:
    value = turn.get("completedAt")
    if (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and 0 <= value <= 253402300799
        and math.isfinite(value)
    ):
        return float(value)
    return None


def _history_turns(result: Mapping[str, object]) -> Sequence[object]:
    data = result.get("data")
    if not isinstance(data, (list, tuple)):
        raise RuntimeConnectionError("thread/turns/list returned no usable turn page")
    if len(data) > CODEX_RUNTIME_RECONCILE_PAGE_SIZE:
        raise RuntimeConnectionError("thread/turns/list exceeded the requested page bound")
    return data


def _fact(
    *,
    kind: TrajectoryKind,
    summary: str,
    native_id: str | None,
    turn_id: str | None,
    status: TrajectoryStatus,
    lane: TrajectoryLane | None = None,
) -> object:
    from theater.harness.contracts.trajectory import TrajectoryFact

    return TrajectoryFact(
        kind=kind,
        summary=summary,
        source="codex-live",
        lane=lane,
        status=status,
        native_id=native_id,
        turn_id=turn_id,
    )


def _native_revision(item: Mapping[str, object]) -> int:
    """The bounded, non-negative native revision of one completed item."""
    revision = item.get("revision")
    if type(revision) is not int or revision < 0:
        return 0
    return min(revision, CODEX_RUNTIME_REVISION_MAX)


def _user_message_text(content: object) -> str:
    if not isinstance(content, (list, tuple)):
        return ""
    parts = [
        part.get("text")
        for part in content
        if isinstance(part, Mapping) and part.get("type") == "text"
    ]
    return "\n".join(text for text in parts if isinstance(text, str))


def _agent_message_text(items: object) -> str | None:
    if not isinstance(items, (list, tuple)):
        return None
    parts: list[str] = []
    for item in items:
        if not isinstance(item, Mapping) or item.get("type") != "agentMessage":
            continue
        text = item.get("text")
        if isinstance(text, str) and text:
            parts.append(text)
    return "\n".join(parts) if parts else None


def _summary_view_carries_exact_final_message(
    terminal: NativeTurnTerminal, items_view: object
) -> bool:
    """Whether a summary item view is guaranteed to be the exact final result."""
    return terminal is NativeTurnTerminal.COMPLETED and items_view == "summary"


def _item_summary(item: Mapping[str, object]) -> str | None:
    item_type = item.get("type")
    if not isinstance(item_type, str) or not item_type:
        return None
    label = f"codex item: {item_type}"
    command = item.get("command")
    if isinstance(command, str) and command:
        return f"{label} {command[:160]}"
    return label


def _clarification_details(questions: Sequence) -> str:
    titles: list[str] = []
    for question in questions:
        if not isinstance(question, Mapping):
            continue
        for field in ("question", "title", "header"):
            value = question.get(field)
            if isinstance(value, str) and value:
                titles.append(value)
                break
    return " | ".join(titles)[:240] if titles else "clarification questions"


def _turn_error_message(error: object) -> str | None:
    if not isinstance(error, Mapping):
        return None
    message = error.get("message")
    return message if isinstance(message, str) and message else None


def _thread_status_type(thread: Mapping[str, object]) -> str | None:
    status = thread.get("status")
    if isinstance(status, Mapping) and isinstance(status.get("type"), str):
        return status["type"]
    return None


def _seconds_from_ms(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) / 1000.0


def _version_from_user_agent(user_agent: object) -> str | None:
    if not isinstance(user_agent, str):
        return None
    head = user_agent.split(" ", 1)[0]
    if "/" not in head:
        return None
    version = head.rsplit("/", 1)[-1]
    return version if version and version[0].isdigit() else None
