"""Internal audit of strict-contract failures: a durable row plus a bounded counter."""

from __future__ import annotations

import contextlib
import json
import math
from typing import Any

from theater.constants.observability import RESPONSE_CONTRACT_FAILURE_METRIC
from theater.constants.response_contract import (
    CONTRACT_AUDIT_DETAIL_MAX_CHARS,
    CONTRACT_ERRORS_MAX,
    CONTRACT_FAILURE_REASONS,
    CONTRACT_ISSUE_KEYWORDS,
    CONTRACT_ISSUE_PATH_MAX_CHARS,
    CONTRACT_MEASURED_KEYWORDS,
)
from theater.models import Job, now
from theater.observability.engine import metric_bridge
from theater.observability.metrics import MetricKind, MetricSpec

REPAIR_STARTED = "repair_started"
REPAIR_SUCCEEDED = "repair_succeeded"
UNSUPPORTED_ROUTE = "unsupported_route"
CONTRACT_UNAVAILABLE = "contract_unavailable"
REPAIR_BLOCKED = "repair_blocked"
REPAIR_REJECTED = "repair_rejected"
REPAIR_UNKNOWN = "repair_unknown"
REPAIR_TURN_FAILED = "repair_turn_failed"
REPAIR_TURN_CONFLICT = "repair_turn_conflict"
REPAIR_EXHAUSTED = "repair_exhausted"
REPAIR_TIMEOUT = "repair_timeout"
REPAIR_INTERRUPTED = "repair_interrupted"

#: Events that mean the caller got ``unable_to_parse``; only these are counted.
FAILURE_EVENTS = frozenset(
    {
        UNSUPPORTED_ROUTE,
        CONTRACT_UNAVAILABLE,
        REPAIR_BLOCKED,
        REPAIR_REJECTED,
        REPAIR_UNKNOWN,
        REPAIR_TURN_FAILED,
        REPAIR_TURN_CONFLICT,
        REPAIR_EXHAUSTED,
        REPAIR_TIMEOUT,
    }
)


def _issue(value: dict) -> dict[str, str | int | float] | None:
    path, keyword = value.get("path"), value.get("keyword")
    if (
        not isinstance(path, str)
        or len(path) > CONTRACT_ISSUE_PATH_MAX_CHARS
        or not isinstance(keyword, str)
        or keyword not in CONTRACT_ISSUE_KEYWORDS
    ):
        return None
    issue: dict[str, str | int | float] = {"path": path, "keyword": keyword}
    keys = (
        ("limit", "actual")
        if keyword in CONTRACT_MEASURED_KEYWORDS
        else (("position",) if keyword == "json" else ())
    )
    for key in keys:
        number = value.get(key)
        if (
            isinstance(number, (int, float))
            and not isinstance(number, bool)
            and (isinstance(number, int) or math.isfinite(number))
            and number >= 0
        ):
            issue[key] = number
    return issue


def diagnostic(
    event: str,
    reason: str | None = None,
    issues: tuple[dict[str, str | int | float], ...] = (),
    truncated: bool = False,
) -> str:
    """Serialize whole safe issues within the audit column's clipping bound."""
    kept: list[dict[str, str | int | float]] = []
    row: dict[str, object] = {
        "v": 1,
        "event": event,
        "reason": reason if reason in CONTRACT_FAILURE_REASONS else None,
        "issues": kept,
    }
    safe = [_issue(issue) for issue in issues[:CONTRACT_ERRORS_MAX]]
    was_truncated = (
        truncated or len(issues) > CONTRACT_ERRORS_MAX or any(issue is None for issue in safe)
    )
    for issue in safe:
        if issue is None:
            continue
        candidate = {**row, "issues": [*kept, issue], "truncated": True}
        if (
            len(json.dumps(candidate, sort_keys=True, separators=(",", ":"), allow_nan=False))
            > CONTRACT_AUDIT_DETAIL_MAX_CHARS
        ):
            was_truncated = True
            break
        kept.append(issue)
    if was_truncated:
        row["truncated"] = True
    return json.dumps(row, sort_keys=True, separators=(",", ":"), allow_nan=False)


def project(store: Any, handle: str) -> dict[str, object] | None:
    """Project a failed strict job from indexed audit rows; never echo legacy detail."""
    last: dict[str, object] | None = None
    validated: dict[str, object] | None = None
    for event, detail in store.contract_audit_for_job(handle):
        decoded = None
        if detail is not None and len(detail) <= CONTRACT_AUDIT_DETAIL_MAX_CHARS:
            try:
                value = json.loads(detail)
            except ValueError:
                value = None
            if isinstance(value, dict) and value.get("v") == 1 and value.get("event") == event:
                reason, issues = value.get("reason"), value.get("issues")
                if (
                    (
                        reason is None
                        or (isinstance(reason, str) and reason in CONTRACT_FAILURE_REASONS)
                    )
                    and isinstance(issues, list)
                    and len(issues) <= CONTRACT_ERRORS_MAX
                ):
                    safe = [_issue(issue) for issue in issues if isinstance(issue, dict)]
                    if len(safe) == len(issues) and all(issue is not None for issue in safe):
                        decoded = {"event": event, "reason": reason, "issues": safe}
                        if value.get("truncated") is True:
                            decoded["truncated"] = True
        if decoded is not None and decoded["reason"] is not None:
            validated = decoded
        if event in FAILURE_EVENTS:
            last = decoded or {"event": event, "reason": None, "issues": []}
    if last is None:
        return None
    if last["reason"] is None and validated is not None:
        last = {**validated, "event": last["event"]}
    return last


_FAILURE_SPEC = MetricSpec(
    RESPONSE_CONTRACT_FAILURE_METRIC,
    "Strict response contracts that ended unable_to_parse, by internal reason.",
    "1",
    MetricKind.COUNTER,
    ("reason",),
)


def register_metric_specs() -> None:
    with contextlib.suppress(Exception):
        bridge = metric_bridge()
        if bridge is not None:
            bridge.register_specs((_FAILURE_SPEC,))


def record(
    store: Any,
    job: Job,
    event: str,
    detail: str | None = None,
    *,
    reason: str | None = None,
    issues: tuple[dict[str, str | int | float], ...] = (),
    truncated: bool = False,
    connection=None,
) -> None:
    """Append one audit row for ``job``; failures also tick the bounded counter."""
    stored = (
        diagnostic(event, reason, issues, truncated)
        if event in FAILURE_EVENTS or reason is not None
        else detail
    )
    store.record_contract_audit(
        job.handle, job.target_id, event, stored, at=now(), connection=connection
    )
    if event in FAILURE_EVENTS:
        with contextlib.suppress(Exception):
            bridge = metric_bridge()
            if bridge is not None:
                bridge.observe(_FAILURE_SPEC, 1, {"reason": event})
