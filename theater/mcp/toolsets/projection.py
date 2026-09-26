"""Agent-facing projections: drop what the caller already knows and every empty field.

Agents re-read these on every poll, so each byte costs context repeatedly.
"""

from __future__ import annotations

from collections.abc import Mapping

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
    presence = entry.get("human_presence")
    if isinstance(presence, Mapping):
        if presence.get("state") == "absent" and not presence.get("protected"):
            del entry["human_presence"]
        else:
            entry["human_presence"] = compact(presence, drop=_PRESENCE_INTERNAL)
    return entry


__all__ = ["compact", "job_entry"]
