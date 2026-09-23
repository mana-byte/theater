"""Formatting for public controls and durable control-operation outcomes."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from regie.controllers.actions import ActionRecord, OperationController
from regie.ui_constants import REGIE_CONTROLS_REPORT_LINE_MAX, REGIE_CONTROLS_REPORT_MAX_LINES
from theater.frontend import Controls, FrontendClient

_UNKNOWN_OUTCOME_SUFFIX = " — do not retry blindly; the result may remain unknowable"
_ACCEPTED_DELIVERY = frozenset({"accepted", "delivered", "dispatched"})
_REJECTED_DELIVERY = frozenset({"rejected", "refused"})


class ControlController(OperationController):
    """Named controller for queue, settings, and interrupt public operations."""

    def __init__(self, client: FrontendClient) -> None:
        super().__init__(client)


def _bounded(value: object) -> str:
    text = " ".join(str(value).split())
    if len(text) > REGIE_CONTROLS_REPORT_LINE_MAX:
        return f"{text[: REGIE_CONTROLS_REPORT_LINE_MAX - 1]}…"
    return text


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _result_detail(record: ActionRecord) -> str:
    result = _mapping(record.result)
    parts: list[str] = []
    for value in (
        result.get("reason"),
        result.get("detail"),
        result.get("phase"),
        record.phase,
    ):
        if isinstance(value, str):
            rendered = _bounded(value)
            if (
                rendered
                and rendered not in parts
                and rendered
                not in {
                    "delivery_acknowledged",
                    "delivery_rejected",
                    "delivery_unknown",
                    "operation_completed",
                }
            ):
                parts.append(rendered)
    return f" ({'; '.join(parts)})" if parts else ""


def _error_detail(record: ActionRecord, result: Mapping[str, object]) -> str:
    parts: list[str] = []
    for value in (
        result.get("error_code"),
        result.get("error"),
        record.error_code,
        record.detail,
    ):
        if isinstance(value, str):
            rendered = _bounded(value)
            if rendered and rendered not in parts:
                parts.append(rendered)
    return f" ({'; '.join(parts)})" if parts else _result_detail(record)


def _delivery_of(record: ActionRecord, result: Mapping[str, object]) -> str:
    """Return explicit receipt evidence, then recognised durable phases."""
    for key in ("delivery", "status", "phase"):
        value = result.get(key)
        if isinstance(value, str) and value:
            return value
    return {
        "delivery_acknowledged": "accepted",
        "delivery_rejected": "rejected",
        "delivery_unknown": "unknown",
    }.get(record.phase or "", "")


def _action_label(action: str) -> str:
    return {
        "queue_followup": "queue followup",
        "settings_update": "settings update",
    }.get(action, action)


def _negative_delivery(
    record: ActionRecord,
    result: Mapping[str, object],
) -> tuple[str, Literal["warning", "error"]] | None:
    """Render delivery states that must override otherwise-positive evidence."""
    delivery = _delivery_of(record, result)
    label = _action_label(record.action)
    detail = _result_detail(record)
    if delivery in _REJECTED_DELIVERY:
        return f"{label} rejected{detail}", "error"
    if delivery == "unknown":
        return f"{label} delivery unknown{_UNKNOWN_OUTCOME_SUFFIX}{detail}", "warning"
    if delivery == "pending":
        return f"{label} pending — delivery not confirmed yet{detail}", "warning"
    return None


def describe_action(
    record: ActionRecord,
) -> tuple[str, Literal["information", "warning", "error"] | None]:
    """Describe a durable action without discarding its delivery/result fields."""
    action = record.action
    result = _mapping(record.result)
    detail = f": {_bounded(record.detail)}" if record.detail else ""
    if record.state.value == "pending":
        handle = (
            f" as {record.job_handle}" if action == "queue_followup" and record.job_handle else ""
        )
        return f"{action} pending{handle}", None
    if record.state.value == "refused":
        return f"{action} refused{detail}", "warning"
    if record.state.value == "uncertain":
        return f"{action} outcome uncertain{_UNKNOWN_OUTCOME_SUFFIX}{detail}", "warning"
    if record.state.value == "failed":
        return f"{action} failed{detail}", "error"

    return _describe_success(record, result)


def _describe_success(
    record: ActionRecord,
    result: Mapping[str, object],
) -> tuple[str, Literal["information", "warning", "error"]]:
    action = record.action
    negative = _negative_delivery(record, result)
    if negative is not None:
        return negative
    if action == "queue_followup":
        return _describe_queue(record, result)
    if action == "interrupt":
        return _describe_interrupt(record, result)
    if action == "settings_update":
        return _describe_settings(record, result)
    if action == "send":
        return _describe_delivery_action(record, result)
    return f"{action} succeeded{_result_detail(record)}", "information"


def _describe_queue(
    record: ActionRecord,
    result: Mapping[str, object],
) -> tuple[str, Literal["information", "warning"]]:
    delivery = _delivery_of(record, result)
    detail = _result_detail(record)
    if record.job_handle:
        return f"followup queued as {record.job_handle}{detail}", "information"
    if delivery in _ACCEPTED_DELIVERY:
        return f"queue followup accepted{detail}", "information"
    if delivery:
        return f"queue followup {delivery}{detail}", "information"
    return f"queue followup delivery unknown{_UNKNOWN_OUTCOME_SUFFIX}{detail}", "warning"


def _describe_interrupt(
    record: ActionRecord,
    result: Mapping[str, object],
) -> tuple[str, Literal["information", "warning"]]:
    delivery = _delivery_of(record, result)
    interrupted = result.get("interrupted")
    if interrupted is False:
        reason = result.get("reason") or record.phase
        suffix = f" — {_bounded(reason)}" if reason else ""
        if reason in {"already_idle", "already_not_working"}:
            return f"nothing to interrupt{suffix}", "information"
        return f"interrupt not performed{suffix}", "warning"
    if interrupted is True or delivery in _ACCEPTED_DELIVERY:
        return f"interrupted{_result_detail(record)}", "information"
    if delivery:
        return f"interrupt {delivery}{_result_detail(record)}", "information"
    return (
        f"interrupt delivery unknown{_UNKNOWN_OUTCOME_SUFFIX}{_result_detail(record)}",
        "warning",
    )


def _describe_settings(
    record: ActionRecord,
    result: Mapping[str, object],
) -> tuple[str, Literal["information", "warning", "error"]]:
    delivery = _delivery_of(record, result)
    applied = result.get("applied")
    if applied is False:
        return f"settings update refused{_error_detail(record, result)}", "error"
    if applied is True or delivery in _ACCEPTED_DELIVERY:
        return f"settings updated{_result_detail(record)}", "information"
    if delivery:
        return f"settings update {delivery}{_result_detail(record)}", "information"
    return (
        f"settings update outcome unknown{_UNKNOWN_OUTCOME_SUFFIX}{_error_detail(record, result)}",
        "warning",
    )


def _describe_delivery_action(
    record: ActionRecord,
    result: Mapping[str, object],
) -> tuple[str, Literal["information", "warning"]]:
    delivery = _delivery_of(record, result)
    if delivery in _ACCEPTED_DELIVERY:
        return f"{record.action} accepted{_result_detail(record)}", "information"
    if delivery:
        return f"{record.action} {delivery}{_result_detail(record)}", "information"
    return (
        f"{record.action} delivery unknown{_UNKNOWN_OUTCOME_SUFFIX}{_result_detail(record)}",
        "warning",
    )


def _report_prefix(extra: Mapping[str, object]) -> list[str]:
    lines: list[str] = []
    wiring = extra.get("wiring")
    if isinstance(wiring, str) and wiring:
        lines.append(f"wiring: {_bounded(wiring)}")
    health = extra.get("health")
    if isinstance(health, str) and health:
        lines.append(f"health: {_bounded(health)}")
    elif isinstance(health, Mapping):
        connection = health.get("connection")
        if isinstance(connection, str) and connection:
            lines.append(f"health: connection={_bounded(connection)}")
        diagnostics = health.get("diagnostics")
        if isinstance(diagnostics, (list, tuple)) and diagnostics:
            shown = "; ".join(_bounded(item) for item in diagnostics[:5])
            lines.append(f"diagnostics: {shown}")
    return lines


def _queued_handle(item: object) -> str:
    if isinstance(item, Mapping):
        handle = item.get("handle")
        if isinstance(handle, str) and handle:
            return handle
    return str(item) if item is not None else ""


def _report_suffix(extra: Mapping[str, object]) -> list[str]:
    lines: list[str] = []
    settings = extra.get("settings")
    if isinstance(settings, Mapping) and settings:
        shown = ", ".join(f"{key}={_bounded(value)}" for key, value in settings.items())
        lines.append(f"settings: {shown}")
    turn = extra.get("active_turn")
    if isinstance(turn, Mapping) and turn:
        identifier = turn.get("native_turn_id") or turn.get("id")
        lines.append("active turn: " + (_bounded(identifier) if identifier else "yes"))
    elif isinstance(turn, str) and turn:
        lines.append(f"active turn: {_bounded(turn)}")
    queued = extra.get("queued", extra.get("queued_handles"))
    if isinstance(queued, (list, tuple)):
        handles = ", ".join(_bounded(_queued_handle(item)) for item in queued[:5])
        lines.append(f"queued followups: {len(queued)}" + (f" ({handles})" if handles else ""))
    return lines


def format_controls_report(controls: Controls) -> str:
    """Render the typed control report in the compact rc9 presentation."""
    lines = _report_prefix(controls.extra)
    for name, capability in controls.actions.items():
        available = capability.supported and capability.route_available and capability.admissible
        if available:
            lines.append(f"{name}: available")
            continue
        reason = capability.reason or "unavailable"
        suffix = f" ({_bounded(capability.detail)})" if capability.detail else ""
        lines.append(f"{name}: unavailable — {_bounded(reason)}{suffix}")
    lines.extend(_report_suffix(controls.extra))
    if not lines:
        return "the daemon reported no controls"
    return "\n".join(_bounded(line) for line in lines[:REGIE_CONTROLS_REPORT_MAX_LINES])


__all__ = [
    "ActionRecord",
    "ControlController",
    "describe_action",
    "format_controls_report",
]
