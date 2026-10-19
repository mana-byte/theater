"""Validated job links, task generations and the compact progress view.

One owner for the link rules: association shape, duplicate and cap handling,
issuance authorization from job facts, generation boundaries, current-work and
latest-review selection, and review artifact evidence. Beads task metadata is
the only persistence; no daemon storage is involved.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from typing import Any

from theater.json_values import decode_finite_json, finite_json_value

from .backend import BeadsError, Board
from .projection import _blockers, _failed_prerequisites, _summary

LINKS_KEY = "theater_links"
GENERATION_KEY = "theater_generation"
BOUNDARY_KEY = "theater_boundary"
BOUNDARY_FALLBACK_KEY = "theater_boundary_fallback"
CLOSED_AT_KEY = "theater_closed_at"

MAX_LINKS = 100
FACTS_BATCH = 50
LINK_VERSION = 1
MAX_HANDLE_CHARS = 256
ROLES = ("work", "review")


def _flag(value: object) -> bool:
    """Strict JSON-true: only the boolean True passes, never a truthy string."""
    return isinstance(value, bool) and value


def handle_text(value: object, field: str = "handle") -> str:
    """An exact opaque job handle; never trimmed, parsed or matched by suffix."""
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_HANDLE_CHARS:
        raise BeadsError(
            f"{field} must be an exact job handle of at most {MAX_HANDLE_CHARS} characters"
        )
    return value


def _metadata(row: dict[str, Any]) -> dict[str, Any]:
    metadata = row.get("metadata") or {}
    return metadata if isinstance(metadata, dict) else {}


def _number(value: object) -> int | float | None:
    """A finite JSON number; bd parses scalar metadata but keeps complex values as text."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value if finite_json_value(value) else None
    if isinstance(value, str):
        ok, parsed = decode_finite_json(value)
        if ok and not isinstance(parsed, bool) and isinstance(parsed, (int, float)):
            return parsed
    return None


def _integer(value: object) -> int | None:
    """A generation counter; bd stores scalar integers natively."""
    parsed = _number(value)
    if isinstance(parsed, int):
        return parsed
    return None


def _corrupt(task_id: object, detail: str) -> BeadsError:
    """Fail closed on stored metadata we cannot trust; never normalize it away."""
    return BeadsError(
        f"task {task_id} stores unsupported or corrupt job-link metadata ({detail}); "
        "refusing to read or change its links"
    )


def _link_records(metadata: dict[str, Any], task_id: object) -> list[dict[str, Any]]:
    """Stored links in stored order; any record we cannot trust fails closed."""
    if LINKS_KEY not in metadata:
        return []
    raw = metadata[LINKS_KEY]
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError as exc:
            raise _corrupt(task_id, f"{LINKS_KEY} is not valid JSON") from exc
    if not isinstance(raw, list):
        raise _corrupt(task_id, f"{LINKS_KEY} is not a list")
    if len(raw) > MAX_LINKS:
        raise _corrupt(task_id, f"{LINKS_KEY} holds {len(raw)} records over the cap of {MAX_LINKS}")
    records: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise _corrupt(task_id, f"{LINKS_KEY} record {index} is not an object")
        records.append(_validated_record(item, task_id, index))
    return records


def _validated_record(item: dict[str, Any], task_id: object, index: int) -> dict[str, Any]:
    """One stored record checked field by field; untrusted data fails closed."""
    label = f"{LINKS_KEY} record {index}"
    version = item.get("version")
    if type(version) is not int or version != LINK_VERSION:
        raise _corrupt(task_id, f"{label} has version {version!r}")
    handle, role, review_of = item.get("handle"), item.get("role"), item.get("review_of")
    if not isinstance(handle, str) or not handle.strip() or len(handle) > MAX_HANDLE_CHARS:
        raise _corrupt(task_id, f"{label} has an unusable handle")
    if role not in ROLES:
        raise _corrupt(task_id, f"{label} has role {role!r}")
    if (role == "work") != (review_of is None):
        raise _corrupt(task_id, f"{label} has an incompatible review_of")
    if review_of is not None and (
        not isinstance(review_of, str) or not review_of.strip() or len(review_of) > MAX_HANDLE_CHARS
    ):
        raise _corrupt(task_id, f"{label} has an unusable review_of")
    created_at = _number(item.get("created_at"))
    if created_at is None:
        raise _corrupt(task_id, f"{label} has an unusable creation time")
    generation = _integer(item.get("generation"))
    if generation is None or generation < 0:
        raise _corrupt(task_id, f"{label} has an unusable generation")
    return {
        "version": LINK_VERSION,
        "handle": handle,
        "role": role,
        "review_of": review_of,
        "generation": generation,
        "created_at": created_at,
    }


def task_state(row: dict[str, Any]) -> dict[str, Any]:
    """Links plus the generation bookkeeping carried in this task's metadata."""
    metadata = _metadata(row)
    task_id = row.get("id")
    generation = 0
    if GENERATION_KEY in metadata:
        stored = _integer(metadata[GENERATION_KEY])
        if stored is None or stored < 0:
            raise _corrupt(task_id, f"{GENERATION_KEY} is unusable")
        generation = stored
    boundary = None
    if BOUNDARY_KEY in metadata:
        boundary = _number(metadata[BOUNDARY_KEY])
        if boundary is None:
            raise _corrupt(task_id, f"{BOUNDARY_KEY} is unusable")
    fallback = False
    if BOUNDARY_FALLBACK_KEY in metadata:
        flag = metadata[BOUNDARY_FALLBACK_KEY]
        if isinstance(flag, bool) or flag not in (1, "1"):
            raise _corrupt(task_id, f"{BOUNDARY_FALLBACK_KEY} is unusable")
        fallback = True
    return {
        "links": _link_records(metadata, task_id),
        "generation": generation,
        "boundary": boundary,
        "boundary_fallback": fallback,
    }


def close_epoch(row: dict[str, Any]) -> int | float | None:
    """When this task closed: recorded first, bd's native closed_at second."""
    metadata = _metadata(row)
    if CLOSED_AT_KEY in metadata:
        recorded = _number(metadata[CLOSED_AT_KEY])
        if recorded is None:
            raise _corrupt(row.get("id"), f"{CLOSED_AT_KEY} is unusable")
        return recorded
    native = row.get("closed_at")
    if not isinstance(native, str) or not native:
        return None
    try:
        moment = datetime.fromisoformat(native)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.timestamp()


def plan_link(
    task_row: dict[str, Any], handle: str, role: str, review_of: str | None
) -> dict[str, Any]:
    """Board-only checks; a verified duplicate short-circuits before any facts read."""
    state = task_state(task_row)
    links = state["links"]
    for record in links:
        if record["handle"] != handle:
            continue
        if record["role"] == role and record["review_of"] == review_of:
            return {"duplicate": True, "record": record, "links_total": len(links)}
        existing = f"{record['handle']} is already linked to this task as {record['role']}"
        if record["review_of"] is not None:
            existing += f" of {record['review_of']}"
        raise BeadsError(existing + "; changing a stored link is not supported")
    if role == "review" and not any(
        record["role"] == "work" and record["handle"] == review_of for record in links
    ):
        raise BeadsError("review_of must name a work job already linked to this task")
    if len(links) >= MAX_LINKS:
        raise BeadsError(
            f"a task keeps at most {MAX_LINKS} job links; existing links cannot be evicted; "
            "track further work on another task"
        )
    return {"duplicate": False, "state": state}


def authorize_new_link(fact: object, instance_id: str, handle: str) -> int | float:
    """Issuance authorization from job facts; returns the immutable creation time."""
    if not isinstance(fact, dict) or not _flag(fact.get("available")):
        raise BeadsError(f"job {handle} is not available; link only jobs this play dispatched")
    play = fact.get("play")
    if not isinstance(play, dict) or play.get("instance_id") != instance_id:
        raise BeadsError(f"job {handle} carries no play proof for this play instance; link refused")
    if not _flag(play.get("caller_seated_at_creation")):
        raise BeadsError(
            f"job {handle}: membership of the issuing caller in this play at creation "
            "is not proven; link refused"
        )
    created_at = _number(fact.get("created_at"))
    if created_at is None:
        raise BeadsError(f"job {handle} has no usable creation time; link refused")
    return created_at


def guard_new_link(
    created_at: int | float,
    role: str,
    state: dict[str, Any],
    row_status: object,
    known_close: int | float | None,
) -> None:
    """Generation and closed-task guards for a new association, after duplicates."""
    boundary = state["boundary"]
    if boundary is not None and created_at < boundary:
        raise BeadsError(
            f"stale generation: job created at {created_at:.3f} precedes this task's "
            f"generation boundary {boundary:.3f}; link a job dispatched after that boundary"
        )
    if role != "work" or row_status != "closed":
        return
    if known_close is None:
        raise BeadsError("task is closed and its close time is unknown; reopen it first, then link")
    if created_at > known_close:
        raise BeadsError("job was created after this task closed; reopen the task first, then link")


def persist_link(
    board: Board,
    task_id: str,
    links: list[dict[str, Any]],
    record: dict[str, Any],
    actor: str,
) -> None:
    """Append one link; --metadata merges per key, preserving all other metadata."""
    board.command(
        "update",
        task_id,
        "--metadata",
        json.dumps({LINKS_KEY: [*links, record]}),
        actor=actor,
    )


def reopen_generation_args(row: dict[str, Any], now: float | None = None) -> list[str]:
    """Generation and boundary half of the native reopen update.

    The boundary is the previous close when it is known, else this reopen time
    with the fallback exposed. Called from the same bd command that reopens.
    """
    state = task_state(row)
    previous_close = close_epoch(row)
    fallback = previous_close is None
    if fallback:
        previous_close = time.time() if now is None else now
    generation = state["generation"] + 1
    args = [
        "--set-metadata",
        f"{GENERATION_KEY}={generation}",
        "--set-metadata",
        f"{BOUNDARY_KEY}={previous_close!r}",
    ]
    if fallback:
        args.extend(("--set-metadata", f"{BOUNDARY_FALLBACK_KEY}=1"))
    else:
        args.extend(("--unset-metadata", BOUNDARY_FALLBACK_KEY))
    args.extend(("--unset-metadata", CLOSED_AT_KEY))
    return args


def _selection(links: list[dict[str, Any]], generation: int, role: str) -> dict[str, Any] | None:
    """Newest link of one role in the current generation, by immutable creation time."""
    pool = [
        record for record in links if record["role"] == role and record["generation"] == generation
    ]
    if not pool:
        return None
    newest = max(record["created_at"] for record in pool)
    tied = [record for record in pool if record["created_at"] == newest]
    if len(tied) > 1:
        return {
            "ambiguous": True,
            "created_at": newest,
            "handles": sorted(record["handle"] for record in tied),
        }
    return tied[0]


def _git_summary(fact: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(fact, dict):
        return None
    git = fact.get("git")
    if not isinstance(git, dict):
        return None
    return {"commit_sha": git.get("commit_sha"), "clean": _flag(git.get("clean"))}


def _job_fields(record: dict[str, Any], fact: dict[str, Any] | None) -> dict[str, Any]:
    """Compact projection of one linked job; unavailable facts project as nulls."""
    if not isinstance(fact, dict) or not _flag(fact.get("available")):
        return {
            "handle": record["handle"],
            "created_at": record["created_at"],
            "generation": record["generation"],
            "available": False,
            "state": None,
            "error_code": None,
            "contract_preset": None,
            "delegate_status": None,
            "review_verdict": None,
            "git": None,
        }
    return {
        "handle": record["handle"],
        "created_at": record["created_at"],
        "generation": record["generation"],
        "available": True,
        "state": fact.get("state"),
        "error_code": fact.get("error_code"),
        "contract_preset": fact.get("contract_preset"),
        "delegate_status": fact.get("delegate_status"),
        "review_verdict": fact.get("review_verdict"),
        "git": _git_summary(fact),
    }


def _pinned_clean(fact: dict[str, Any]) -> dict[str, Any] | None:
    git = fact.get("git")
    if not isinstance(git, dict) or git.get("pin_state") != "pinned" or not _flag(git.get("clean")):
        return None
    commit = git.get("commit_sha")
    return git if isinstance(commit, str) and commit else None


def artifact_relation(
    review: dict[str, Any],
    review_fact: dict[str, Any],
    work_fact: dict[str, Any] | None,
    current_work_handle: str | None,
    current_work_ambiguous: bool = False,
) -> str:
    """matched / stale / unverified / unavailable for the current review's evidence."""
    if not _flag(review_fact.get("available")):
        return "unavailable"
    if current_work_ambiguous:
        # An undetermined current work neither supersedes nor matches this review.
        return "unverified"
    if review["review_of"] != current_work_handle:
        # The immutable association proves supersession, whatever became of the
        # old source's facts.
        return "stale"
    if work_fact is None or not _flag(work_fact.get("available")):
        return "unavailable"
    review_ok = (
        review_fact.get("state") == "done"
        and review_fact.get("error_code") is None
        and review_fact.get("structured_status") == "parsed"
        and review_fact.get("contract_preset") == "review"
        and review_fact.get("review_verdict") == "approve"
    )
    work_ok = work_fact.get("state") == "done" and work_fact.get("error_code") is None
    if not review_ok or not work_ok:
        return "unverified"
    handoff = review_fact.get("handoff")
    if handoff is None:
        # No gate: the declared basis needs the review to start after the work ended.
        work_finished = _number(work_fact.get("finished_at"))
        review_created = _number(review_fact.get("created_at"))
        if work_finished is None or review_created is None or review_created <= work_finished:
            return "unverified"
    elif not isinstance(handoff, dict) or handoff.get("source_handle") != review["review_of"]:
        # A gate that is present but unusable or mismatched never matches.
        return "unverified"
    review_git = _pinned_clean(review_fact)
    work_git = _pinned_clean(work_fact)
    if review_git is None or work_git is None or review_git["commit_sha"] != work_git["commit_sha"]:
        return "unverified"
    review_target, work_target = review_fact.get("target_id"), work_fact.get("target_id")
    if not isinstance(review_target, str) or not isinstance(work_target, str):
        return "unverified"
    if review_target == work_target:
        return "unverified"
    return "matched"


def _source_basis(fact: dict[str, Any]) -> str | None:
    """Which provenance basis the review ran on; an unusable handoff reports none."""
    handoff = fact.get("handoff")
    if handoff is None:
        return "declared"
    return "handoff" if isinstance(handoff, dict) else None


def _work_entry(
    selected: dict[str, Any] | None, facts: dict[str, dict[str, Any]]
) -> dict[str, Any] | None:
    if selected is None:
        return None
    if selected.get("ambiguous"):
        return dict(selected)
    return _job_fields(selected, facts.get(selected["handle"]))


def _review_entry(
    selected: dict[str, Any] | None,
    facts: dict[str, dict[str, Any]],
    current_work_handle: str | None,
    current_work_ambiguous: bool = False,
) -> dict[str, Any] | None:
    if selected is None:
        return None
    if selected.get("ambiguous"):
        return dict(selected)
    fact = facts.get(selected["handle"])
    entry = _job_fields(selected, fact)
    entry["review_of"] = selected["review_of"]
    if not isinstance(fact, dict) or not _flag(fact.get("available")):
        entry["source_basis"] = None
        entry["artifact_relation"] = "unavailable"
        return entry
    entry["source_basis"] = _source_basis(fact)
    work_fact = facts.get(selected["review_of"]) if selected["review_of"] else None
    entry["artifact_relation"] = artifact_relation(
        selected, fact, work_fact, current_work_handle, current_work_ambiguous
    )
    return entry


def progress_view(
    board: Board,
    task_id: str,
    row: dict[str, Any],
    rows: dict[str, dict[str, Any]],
    ready: set[str],
    facts: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Compact alternative to detail: links and evidence, never descriptions or notes."""
    state = task_state(row)
    deps, children, blockers = _blockers(board, rows, task_id)
    failed = _failed_prerequisites(rows, deps, children)
    generation = state["generation"]
    current_work = _selection(state["links"], generation, "work")
    work_ambiguous = current_work is not None and bool(current_work.get("ambiguous"))
    if current_work is not None and not work_ambiguous:
        work_handle: str | None = current_work["handle"]
    else:
        work_handle = None
    latest_review = _selection(state["links"], generation, "review")
    entries = []
    for record in state["links"]:
        fact = facts.get(record["handle"])
        entry = _job_fields(record, fact)
        entry["role"] = record["role"]
        entry["review_of"] = record["review_of"]
        if record["role"] == "review":
            if isinstance(fact, dict) and _flag(fact.get("available")):
                entry["source_basis"] = _source_basis(fact)
            else:
                entry["source_basis"] = None
        entries.append(entry)
    return {
        "play_instance_id": board.instance_id,
        **_summary(board, row, rows, ready),
        "blockers": blockers[:200],
        "failed_prerequisites": failed[:200],
        "generation": generation,
        "boundary": state["boundary"],
        "boundary_fallback": state["boundary_fallback"],
        "current_work": _work_entry(current_work, facts),
        "latest_review": _review_entry(latest_review, facts, work_handle, work_ambiguous),
        "job_links": entries,
        "truncated": len(blockers) > 200 or len(failed) > 200,
    }


def facts_handles(links: list[dict[str, Any]]) -> list[str]:
    """Every stored handle in stored order: at most two facts batches per view."""
    return [record["handle"] for record in links]
