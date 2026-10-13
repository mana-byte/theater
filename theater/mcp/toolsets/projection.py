"""Agent-facing projections: drop what the caller already knows and every empty field.

Agents re-read these on every poll, so each byte costs context repeatedly.
"""

from __future__ import annotations

from collections.abc import Mapping

from theater.constants.handoff import ERROR_CANCELLED
from theater.constants.harness import HARNESS_EVENT_TEXT_MAX_CHARS
from theater.constants.observation import TURN_FAILED_CODE
from theater.harness.contracts.events import clip
from theater.json_values import decode_finite_json

#: Longest text clip() emits: the cap plus its "… (+N chars)" marker for an N of up to 12 digits.
_CLIPPED_MAX = HARNESS_EVENT_TEXT_MAX_CHARS + len("… (+999999999999 chars)")

#: Job fields the caller supplied or cannot act on (its own id, its prompt, its schema).
_JOB_ECHO = frozenset(
    {
        "prompt",
        "result",
        "caller_id",
        "target_id",
        "kind",
        "created_at",
        "response_format",
        "actor_client_id",
        "actor_participant_id",
    }
)
#: Presence ordering fields the daemon uses for revision waits, never an agent.
_PRESENCE_INTERNAL = frozenset({"revision", "observed_at"})


def _present(value: object) -> bool:
    return value is not None and value not in ([], {})


def compact(record: Mapping[str, object], *, drop: frozenset[str] = frozenset()) -> dict:
    """Keep only fields that carry a value and that the caller did not supply."""
    return {k: v for k, v in record.items() if k not in drop and _present(v)}


def job_entry(job: Mapping[str, object]) -> dict:
    """One await/send/queue entry: state and outcome, presence only while it matters."""
    entry = compact(job, drop=_JOB_ECHO)
    if entry.get("structured_status") == "parsed":
        raw = entry.get("structured_result")
        if isinstance(raw, str):
            valid, value = decode_finite_json(raw)
            if valid:
                entry["structured_result"] = value
    answer = _final_answer(job)
    if answer:
        entry["result"] = answer
    if job.get("state") in ("crashed", "killed") and not entry.get("error"):
        result = job.get("result")
        if isinstance(result, str) and result:
            entry["error"] = _bounded(result)
    presence = entry.get("human_presence")
    if isinstance(presence, Mapping):
        if presence.get("state") == "absent" and not presence.get("protected"):
            del entry["human_presence"]
        else:
            entry["human_presence"] = compact(presence, drop=_PRESENCE_INTERNAL)
    return entry


def _final_answer(job: Mapping[str, object]) -> str:
    """Keep final answers and the failure details that mapped errors cannot explain."""
    result = job.get("result")
    answers = job.get("state") == "done" or job.get("error_code") in (
        TURN_FAILED_CODE,
        ERROR_CANCELLED,
    )
    if not answers or job.get("structured_status") == "parsed":
        return ""
    return _bounded(result) if isinstance(result, str) else ""


def _bounded(text: str) -> str:
    """Parser-clipped text keeps its original omission count; only longer text is clipped here.

    Length is the only test: a "(+N chars)" suffix in agent text is not clipping provenance.
    """
    return text if len(text) <= _CLIPPED_MAX else clip(text)


__all__ = ["compact", "job_entry"]
