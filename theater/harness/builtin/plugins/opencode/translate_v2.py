"""Project OpenCode 2.x message rows onto the 1.x message and part shapes the observer reads.

2.x embeds parts in one row per message (schema/src/session-message.ts), so each part gets a
positional id; an `idle` marker that closes a turn no step closed becomes its terminal message.
"""

from __future__ import annotations

from .values import _has_tool_calls, _table, _turn_terminal

_PART_WIDTH = 6
_PATH_TOOLS = frozenset({"read", "write", "edit"})
_TOOL_STATUSES = {
    "streaming": "pending",
    "running": "running",
    "completed": "completed",
    "error": "error",
}
_IDLE_ERRORS = {
    "interrupted": {"name": "AbortedError", "message": "OpenCode interrupted the turn"},
    "failed": {"name": "TurnFailed", "message": "OpenCode ended the turn with a failure"},
}
#: A failed step keeps the native prompt loop running mid-turn, like `unknown`.
_FAILED_STEP_FINISH = "unknown"


def part_id(message_id: str, index: int) -> str:
    return f"{message_id}:{index:0{_PART_WIDTH}d}"


def split_part_id(value: str) -> tuple[str, int] | None:
    message_id, sep, index = value.rpartition(":")
    if not sep or not message_id or len(index) != _PART_WIDTH or not index.isdigit():
        return None
    return message_id, int(index)


def message_info(
    message_id: str,
    kind: str,
    session_id: str,
    data: dict,
    *,
    closes_turn: bool = False,
    cause: dict | None = None,
) -> dict:
    """The 1.x `message.data` a 2.x row stands for; non-conversation rows get role `idle`."""
    time_data = _table(data.get("time"))
    info: dict = {
        "id": message_id,
        "sessionID": session_id,
        "time": {"created": time_data.get("created")},
    }
    if kind == "user":
        return {**info, "role": "user"}
    if kind == "assistant":
        return _assistant_info(info, data, time_data)
    if kind == "idle" and closes_turn:
        return _closing_info(info, data, cause)
    return {**info, "role": "idle"}


def message_parts(message_id: str, kind: str, session_id: str, data: dict) -> list[dict]:
    """The 1.x `part.data` rows of one 2.x message, in content order."""
    base = {"messageID": message_id, "sessionID": session_id}
    if kind == "user":
        text = data.get("text")
        if not isinstance(text, str) or not text:
            return []
        return [{"id": part_id(message_id, 0), **base, "type": "text", "text": text}]
    if kind != "assistant":
        return []
    content = data.get("content")
    parts = []
    for index, item in enumerate(content if isinstance(content, list) else ()):
        part = _part(item)
        if part is not None:
            parts.append({"id": part_id(message_id, index), **base, **part})
    return parts


def idle_closes_turn(assistant: tuple[dict, list[dict]] | None, prompted: bool) -> bool:
    """Whether an idle marker is the only boundary its turn has."""
    if assistant is None:
        return prompted
    info, parts = assistant
    return not _turn_terminal(info, _has_tool_calls(parts))


def _assistant_info(info: dict, data: dict, time_data: dict) -> dict:
    info = {**info, "role": "assistant"}
    completed = time_data.get("completed")
    if completed is not None:
        info["time"] = {**info["time"], "completed": completed}
    model = _table(data.get("model"))
    if isinstance(model.get("providerID"), str):
        info["providerID"] = model["providerID"]
    if isinstance(model.get("id"), str):
        info["modelID"] = model["id"]
    if isinstance(data.get("agent"), str):
        info["agent"] = data["agent"]
    for key in ("tokens", "cost"):
        if key in data:
            info[key] = data[key]
    error = _error(data.get("error"))
    if error is not None:
        # 2.x persists a retryable step failure and continues the turn in a new
        # assistant message (runner step.ts Outcome.Continue); only the idle
        # marker ends the turn, so a failed step projects as a continuing one
        # and carries its failure on `stepError`, which never ends anything.
        info["finish"] = _FAILED_STEP_FINISH
        info["stepError"] = error
        return info
    if "finish" in data:
        info["finish"] = data["finish"]
    return info


def _closing_info(info: dict, data: dict, cause: dict | None = None) -> dict:
    created = info["time"]["created"]
    closing = {**info, "role": "assistant", "time": {"created": created, "completed": created}}
    outcome = _string(data.get("outcome"))
    error = _IDLE_ERRORS.get(outcome)
    if error is None:
        return {**closing, "finish": "stop"}
    # The turn's last failed step names the real cause; the generic text is only a fallback.
    return {
        **closing,
        "finish": "error",
        "error": dict(cause if cause and outcome == "failed" else error),
    }


def _error(value: object) -> dict | None:
    """2.x `{type, message}` errors as 1.x `{name, message}`; `aborted` is the interrupt."""
    if not isinstance(value, dict):
        return None
    kind = value.get("type")
    message = value.get("message")
    if kind == "aborted":
        name = "AbortedError"
    else:
        name = kind if isinstance(kind, str) and kind else "Error"
    return {"name": name, "message": message if isinstance(message, str) else ""}


def _part(item: object) -> dict | None:
    if not isinstance(item, dict):
        return None
    kind = item.get("type")
    if kind == "text":
        return {"type": "text", "text": _string(item.get("text"))}
    if kind == "reasoning":
        return {"type": "reasoning", "text": _string(item.get("text")), "time": _span(item)}
    if kind == "tool":
        return _tool(item)
    return None


def _tool(item: dict) -> dict:
    state = _table(item.get("state"))
    name = _string(item.get("name"))
    raw_input = state.get("input")
    tool_input = dict(raw_input) if isinstance(raw_input, dict) else {}
    path = tool_input.get("path")
    # 1.x path projection reads `filePath`; 2.x file tools name it `path`.
    if name in _PATH_TOOLS and isinstance(path, str) and "filePath" not in tool_input:
        tool_input["filePath"] = path
    status = _TOOL_STATUSES.get(_string(state.get("status")), "pending")
    converted: dict = {"status": status, "input": tool_input, "time": _span(item)}
    metadata = dict(_table(state.get("metadata")))
    if status == "completed":
        converted["output"] = _content_text(state.get("content"))
    elif status == "error":
        error = _table(state.get("error"))
        converted["error"] = (
            _string(error.get("message")) or _string(error.get("type")) or "tool failed"
        )
        if error.get("type") == "aborted":
            metadata["interrupted"] = True
    if metadata:
        converted["metadata"] = metadata
    part: dict = {
        "type": "tool",
        "callID": _string(item.get("id")),
        "tool": name,
        "state": converted,
    }
    if item.get("executed") is True:
        part["metadata"] = {"providerExecuted": True}
    return part


def _span(item: dict) -> dict:
    times = _table(item.get("time"))
    start = times.get("ran") or times.get("created")
    end = times.get("completed")
    return {key: value for key, value in (("start", start), ("end", end)) if value is not None}


def _content_text(value: object) -> str:
    if not isinstance(value, list):
        return ""
    return "\n".join(
        item["text"]
        for item in value
        if isinstance(item, dict)
        and item.get("type") == "text"
        and isinstance(item.get("text"), str)
    )


def _string(value: object) -> str:
    return value if isinstance(value, str) else ""


__all__ = [
    "idle_closes_turn",
    "message_info",
    "message_parts",
    "part_id",
    "split_part_id",
]
