"""Formatting for public controls and durable control-operation outcomes."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from regie.controllers.actions import ActionRecord, OperationController
from regie.ui_constants import REGIE_CONTROLS_REPORT_LINE_MAX, REGIE_CONTROLS_REPORT_MAX_LINES
from theater.frontend import Controls, FrontendClient

_UNKNOWN_OUTCOME_SUFFIX = " — do not retry blindly; the result may remain unknowable"


class ControlController(OperationController):
    """Named controller for steer, queue, settings, and interrupt public operations."""

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
    for value in (result.get("reason"), result.get("detail"), record.phase):
        if isinstance(value, str):
            rendered = _bounded(value)
            if (
                rendered
                and rendered not in parts
                and rendered
                not in {
                    "delivery_acknowledged",
                    "operation_completed",
                }
            ):
                parts.append(rendered)
    return f" ({'; '.join(parts)})" if parts else ""


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

    if action == "queue_followup" and record.job_handle:
        return f"followup queued as {record.job_handle}{_result_detail(record)}", "information"
    if action == "interrupt":
        if result.get("interrupted") is False or record.phase == "already_idle":
            reason = result.get("reason") or record.phase
            suffix = f" — {_bounded(reason)}" if reason else ""
            return f"nothing to interrupt{suffix}", "information"
        return f"interrupted{_result_detail(record)}", "information"
    if action == "settings_update":
        applied = result.get("applied")
        if applied is False:
            return f"settings update refused{_result_detail(record)}", "error"
        return f"settings updated{_result_detail(record)}", "information"
    if action in {"send", "steer"}:
        delivery = result.get("delivery")
        if isinstance(delivery, str) and delivery:
            return f"{action} {delivery}{_result_detail(record)}", "information"
        return f"{action} accepted{_result_detail(record)}", "information"
    return f"{action} succeeded{_result_detail(record)}", "information"


def _extra_lines(extra: Mapping[str, object]) -> list[str]:
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
    settings = extra.get("settings")
    if isinstance(settings, Mapping) and settings:
        shown = ", ".join(f"{key}={_bounded(value)}" for key, value in settings.items())
        lines.append(f"settings: {shown}")
    queued = extra.get("queued", extra.get("queued_handles"))
    if isinstance(queued, (list, tuple)):
        handles = ", ".join(_bounded(item) for item in queued[:5])
        lines.append(f"queued followups: {len(queued)}" + (f" ({handles})" if handles else ""))
    return lines


def format_controls_report(controls: Controls) -> str:
    """Render the typed control report in the compact rc9 presentation."""
    lines = _extra_lines(controls.extra)
    for name, capability in controls.actions.items():
        available = capability.supported and capability.route_available and capability.admissible
        if available:
            lines.append(f"{name}: available")
            continue
        reason = capability.reason or "unavailable"
        suffix = f" ({_bounded(capability.detail)})" if capability.detail else ""
        lines.append(f"{name}: unavailable — {_bounded(reason)}{suffix}")
    if not lines:
        return "the daemon reported no controls"
    return "\n".join(_bounded(line) for line in lines[:REGIE_CONTROLS_REPORT_MAX_LINES])


__all__ = [
    "ActionRecord",
    "ControlController",
    "describe_action",
    "format_controls_report",
]
