"""The recall segment reader: explains a job segment (handle) or a gap segment.
Jobs read transcripts via ``open_source``; gaps are the only place allowed to fork ``git log``.
Read-only: brief-derived text must never feed back into the index.
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import replace
from pathlib import Path

from sqlalchemy import select

from theater.constants.daemon import (
    RECALL_READ_RESPONSE_MAX_BYTES,
    TRANSCRIPT_READABLE_KINDS,
)
from theater.daemon import workers
from theater.daemon.observation.process import observation_process_id
from theater.daemon.observer import history_correlation_is_ambiguous
from theater.daemon.recall_history import read_history_page
from theater.daemon.schema import jobs, touch
from theater.daemon.touch_paths import is_canonical_touch_path
from theater.harness import HARNESSES, normalize
from theater.harness.contracts.source import History
from theater.models import BadRequest
from theater.provenance import is_trusted_provenance
from theater.transcript_identity import (
    TRANSCRIPT_IDENTITY_LOST_CODE,
    transcript_identity_recovery_message,
)

logger = logging.getLogger("theater.recall.read")

#: Kinds in a job segment's transcript; ERROR dropped (same filter as transcripts._READABLE).
_READABLE = TRANSCRIPT_READABLE_KINDS

#: Ceiling on git log output for a gap segment; caller gets a truncation note.
_MAX_GAP_COMMITS = 200

#: How long a git log for a gap segment may run before being killed; prevents stalling the daemon.
_GIT_TIMEOUT = 10


async def read_segment(
    segment_id: str,
    *,
    store,
    registry,
    cwd: str,
    observer=None,
) -> dict:
    """Explain what happened inside one timeline segment.
    Never raises for missing transcripts or unexplainable history: return what the database
    remembers.
    """
    if segment_id.startswith("gap:"):
        brief = await workers.to_thread(_read_gap, segment_id, cwd=cwd, label="recall_read.gap")
    else:
        brief = await _read_job(segment_id, store=store, registry=registry, observer=observer)
    await workers.to_thread(_apply_response_budget, brief, label="recall_read.response_budget")
    return brief


def _apply_response_budget(brief: dict) -> None:
    """Keep the newest events from a bounded source page within the MCP frame cap.

    Older material remains available through ``read_transcript`` paging.
    """
    if _encoded_size(brief) <= RECALL_READ_RESPONSE_MAX_BYTES:
        return
    transcript = brief.get("transcript")
    if not isinstance(transcript, dict):
        return
    events = transcript.get("events")
    if not isinstance(events, list) or not events:
        return  # nothing compressible; nothing to clip
    original = len(events)
    transcript["events"] = []
    # Provisional truncation facts, so the fit below accounts for their own
    # size: a note added after the fit is what pushes a brief back over.
    transcript["truncated"] = True
    transcript["dropped_events"] = None if transcript.get("has_older") else original
    transcript["truncation_note"] = _truncation_note(0)
    base = _encoded_size(brief)
    sizes = [_encoded_size(event) for event in events]
    remaining = RECALL_READ_RESPONSE_MAX_BYTES - base
    # Each list member costs its own dict plus one comma beyond the empty list.
    total = 0
    keep_from = len(events)
    for index in range(len(events) - 1, -1, -1):
        total += sizes[index] + 2
        if total > remaining:
            break
        keep_from = index
    if keep_from < len(events):
        transcript["events"] = events[keep_from:]
    else:
        # Even the newest event alone exceeds the budget: keep it and clip
        # its text instead of returning an empty explanation. The older
        # events cannot fit and are dropped with the truncation facts.
        newest = events[-1] if isinstance(events[-1], dict) else None
        transcript["events"] = events[-1:]
        if newest is not None:
            _clip_event_text(newest, remaining)
    # The suffix arithmetic is conservative; this loop makes the budget
    # contract unconditional, whatever the framing costs really were. The
    # truncation facts are recomputed each pass so they stay truthful.
    while transcript["events"]:
        transcript["dropped_events"] = (
            None if transcript.get("has_older") else original - len(transcript["events"])
        )
        transcript["truncation_note"] = _truncation_note(len(transcript["events"]))
        if _encoded_size(brief) <= RECALL_READ_RESPONSE_MAX_BYTES:
            return
        transcript["events"].pop(0)
    transcript["dropped_events"] = None if transcript.get("has_older") else original
    transcript["truncation_note"] = _truncation_note(0)


def _truncation_note(kept: int) -> str:
    return (
        f"response budget {RECALL_READ_RESPONSE_MAX_BYTES} bytes kept the newest "
        f"{kept} events; use read_transcript on this participant to page the older material"
    )


def _clip_event_text(event: dict, allowed: int) -> None:
    """Clip one event's text so its serialized form fits ``allowed`` bytes.

    Measured serialized (JSON escaping inflates non-ASCII), marker included.
    """
    text = event.get("text")
    if not isinstance(text, str) or not text:
        return
    original = text
    event["text"] = ""
    event["text_clipped"] = True
    if _encoded_size(event) > allowed:
        del event["text_clipped"]
        event["text"] = original
        return  # the event's own metadata exhausts the allowance; nothing to clip
    while True:
        event["text"] = text + " …[clipped]"
        if _encoded_size(event) <= allowed:
            break
        if len(text) <= 1:
            del event["text_clipped"]
            event["text"] = original
            return  # cannot clip further; keep the honest text
        text = text[: max(1, len(text) // 2)]


def _encoded_size(brief: dict) -> int:
    """The worst-case serialized size of one brief, as re-serialized downstream.

    The MCP bridge may re-encode with ``ensure_ascii``, so the bound is measured against that.
    """
    return len(json.dumps(brief).encode("utf-8"))


# ---- job segments --------------------------------------------------------


async def _read_job(
    handle: str,
    *,
    store,
    registry,
    observer=None,
) -> dict:
    """The brief for a job segment: ``jobs`` metadata plus the transcript via ``open_source``.
    A separate short-lived source, closed in ``finally``, so history reads never move the watcher's
    cursor.
    """
    row = store.conn.execute(select(jobs).where(jobs.c.handle == handle)).first()
    if row is None:
        raise BadRequest(f"no job {handle!r}")
    j = row._mapping

    target_id = j["target_id"]
    try:
        participant = registry.get(target_id) if target_id is not None else None
    except Exception:
        participant = None

    touch_rows = store.conn.execute(
        select(
            touch.c.path,
            touch.c.mode,
            touch.c.sha_before,
            touch.c.sha_after,
            touch.c.sha_before_error,
            touch.c.sha_after_error,
        )
        .where(touch.c.job_handle == handle)
        .order_by(touch.c.path)
    ).fetchall()
    paths = [
        {
            "path": r._mapping["path"],
            "mode": r._mapping["mode"],
            "sha_before": r._mapping["sha_before"],
            "sha_after": r._mapping["sha_after"],
            "sha_before_error": r._mapping["sha_before_error"],
            "sha_after_error": r._mapping["sha_after_error"],
        }
        for r in touch_rows
        if participant is not None
        and participant.cwd is not None
        and is_canonical_touch_path(participant.cwd, r._mapping["path"])
    ]

    brief = {
        "segment": handle,
        "kind": "job",
        "handle": handle,
        "task": j["prompt"],
        "result": j["result"],
        "outcome": j["state"],
        "error_code": j["error_code"],
        "created_at": j["created_at"],
        "finished_at": j["finished_at"],
        "paths": paths,
        "transcript": None,
    }

    # A job whose target was None (CLI spawn, no target) has no transcript to read.
    if target_id is None:
        brief["transcript"] = {
            "available": False,
            "reason": "job has no target participant",
        }
        return brief

    p = participant
    if p is None:
        # The participant was forgotten — the job still happened; only the transcript is gone.
        brief["transcript"] = {
            "available": False,
            "reason": f"participant {target_id} is no longer registered",
        }
        return brief

    brief["harness"] = p.harness
    brief["session_id"] = p.session_id
    brief["cwd"] = p.cwd
    brief["branch"] = p.branch
    brief["parent_id"] = p.parent_id

    checker = getattr(observer, "transcript_identity_lost", None)
    if callable(checker) and checker(p.id):
        brief["transcript"] = {
            "available": False,
            "reason": transcript_identity_recovery_message(p.id),
            "error_code": TRANSCRIPT_IDENTITY_LOST_CODE,
        }
        return brief

    harness_name = normalize(p.harness)
    harness = HARNESSES.get(harness_name)
    if harness is None:
        # Harness adapter not loaded — the job metadata survives; only the transcript is unreadable.
        brief["transcript"] = {
            "available": False,
            "reason": f"harness {p.harness!r} is not known",
        }
        return brief

    try:
        history = await workers.to_thread(
            read_history_page,
            harness.observer,
            replace(p),
            observation_process_id(store, p),
            label="recall_read.history_page",
        )
    except Exception:
        logger.debug("reading transcript for %s failed", handle, exc_info=True)
        brief["transcript"] = {
            "available": False,
            "reason": "transcript could not be read",
        }
        return brief

    if history.error_code is not None:
        dead_identity_loss = (
            history.error_code == TRANSCRIPT_IDENTITY_LOST_CODE and p.status.value == "dead"
        )
        brief["transcript"] = {
            "available": False,
            "reason": (
                "trusted dead binding is retained for resume, but its transcript is unavailable"
                if dead_identity_loss
                else transcript_identity_recovery_message(p.id, history.error)
                if history.error_code == TRANSCRIPT_IDENTITY_LOST_CODE
                else history.error or history.error_code
            ),
            "error_code": None if dead_identity_loss else history.error_code,
        }
        return brief

    if history.location is None:
        # The source located nothing — transcript file deleted, or opencode has no session row.
        brief["transcript"] = {
            "available": False,
            "reason": "transcript no longer exists on disk",
        }
        return brief

    if not is_trusted_provenance(history.correlation):
        brief["transcript"] = {
            "available": False,
            "reason": (
                "session is known only from cwd/time; wait for exact/proven correlation "
                "or bind it before reading"
            ),
            "error_code": "transcript_correlation_untrusted",
        }
        return brief

    identity = History(
        location=history.location,
        correlation=history.provenance,
        collision_domain=history.collision_domain,
        pinned=history.pinned,
    )
    if history_correlation_is_ambiguous(registry, p.id, identity):
        brief["transcript"] = {
            "available": False,
            "reason": (
                "session is known only from cwd/time and another retained participant "
                "of the same harness shares that transcript root and cwd"
            ),
            "error_code": "transcript_correlation_ambiguous",
        }
        return brief

    events = [
        {
            "index": event.raw_index,
            "role": str(event.kind),
            "text": event.text or "",
            "tool_name": event.tool_name,
            "turn_end": event.turn_end,
            "turn_terminal": event.turn_terminal,
        }
        for event in history.transcript_events
        if event.kind.value in _READABLE
    ]
    brief["transcript"] = {
        "available": True,
        "location": history.location,
        "events": events,
    }
    if history.has_older:
        brief["transcript"].update(
            has_older=True,
            truncated=True,
            dropped_events=None,
            truncation_note=(
                "recall includes only the newest bounded history page; "
                "use read_transcript on this participant to page older material"
            ),
        )
    return brief


# ---- gap segments --------------------------------------------------------


def _read_gap(segment_id: str, *, cwd: str) -> dict:
    """The brief for a gap segment: commits that moved ``<path>`` from ``<before>`` to ``<after>``.

    The feature's only ``git log`` fork, spent deliberately by a caller who asked.
    """
    # Parse gap:<path>:<before>..<after>; split from the right (path may have colons).
    body = segment_id[len("gap:") :]
    colon = body.rfind(":")
    if colon < 0:
        raise BadRequest(f"malformed gap segment id: {segment_id!r}")
    raw_path = body[:colon]
    sha_part = body[colon + 1 :]
    dotdot = sha_part.find("..")
    if dotdot < 0:
        raise BadRequest(f"malformed gap segment id: {segment_id!r}")
    before_raw = sha_part[:dotdot]
    after_raw = sha_part[dotdot + 2 :]

    # ``-`` is the sentinel for a null sha — convert to None.
    before = None if before_raw == "-" else before_raw
    after = None if after_raw == "-" else after_raw

    # The git root is a hard privacy wall; ``..`` in a path is an attack, not a typo.
    root = _git_root(cwd)
    if root is None:
        return {
            "segment": segment_id,
            "kind": "gap",
            "path": raw_path,
            "sha_before": before,
            "sha_after": after,
            "commits": [],
            "explained": False,
            "note": "cwd is not inside a git repository",
        }

    # Refuse a path that escapes the root; realpath resolves ``..`` against the real filesystem.
    resolved = _resolve_within_root(raw_path, root)
    if resolved is None:
        return {
            "segment": segment_id,
            "kind": "gap",
            "path": raw_path,
            "sha_before": before,
            "sha_after": after,
            "commits": [],
            "explained": False,
            "note": "path escapes the repository root",
        }

    commits = _git_log_for_transition(root, resolved, before, after)
    explained = len(commits) > 0
    note: str | None = None
    if not explained:
        note = "no commit in this repository's history contains that transition"
    elif len(commits) >= _MAX_GAP_COMMITS:
        note = (
            f"output bounded at {_MAX_GAP_COMMITS} commits; the "
            "full history for this path is longer"
        )

    return {
        "segment": segment_id,
        "kind": "gap",
        "path": raw_path,
        "sha_before": before,
        "sha_after": after,
        "commits": commits[:_MAX_GAP_COMMITS],
        "explained": explained,
        **({"note": note} if note else {}),
    }


def _git_root(cwd: str) -> str | None:
    """The main repo root containing ``cwd``, via ``--git-common-dir`` so linked worktrees resolve
    too.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    common_dir = result.stdout.strip()
    if not common_dir:
        return None
    return str(Path(common_dir).parent)


def _resolve_within_root(path: str, root: str) -> str | None:
    """``path`` made repo-relative, or None if it escapes the root.

    The privacy wall is hard: ``..`` is an escape and symlinks are resolved via realpath.
    """
    root_path = Path(root).resolve()
    # Reject ``..`` lexically before resolution; a false accept is a privacy breach.
    if ".." in path.split("/"):
        return None
    candidate = (root_path / path).resolve()
    try:
        candidate.relative_to(root_path)
    except ValueError:
        return None
    return path


def _git_log_for_transition(
    root: str,
    path: str,
    before: str | None,
    after: str | None,
) -> list[dict]:
    """Find commits that touched ``path`` and moved it between two shas (``--find-object``).

    Nothing found is the honest answer; full path history would falsely imply git can explain it.
    """
    fmt = "%H%x1f%an%x1f%ad%x1f%s"
    shas = [s for s in (before, after) if s is not None]
    if not shas:
        # Both shas null — not a real gap; no point forking git.
        return []

    commits: list[dict] = []
    seen: set[str] = set()
    for sha in shas:
        found = _run_git_log(root, fmt, ["--find-object", sha], [path])
        for c in found:
            if c["sha"] not in seen:
                seen.add(c["sha"])
                commits.append(c)
    return commits


def _run_git_log(
    root: str,
    fmt: str,
    extra_args: list[str],
    pathargs: list[str],
) -> list[dict]:
    """One ``git log`` call parsed into commit dicts (``%x1f``-separated); references only, never
    payloads.
    """
    argv = [
        "git",
        "-C",
        root,
        "log",
        f"--format={fmt}",
        f"--max-count={_MAX_GAP_COMMITS}",
    ]
    argv += extra_args
    if pathargs:
        argv += ["--", *pathargs]
    try:
        result = subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        logger.debug("git log for %s failed", pathargs, exc_info=True)
        return []
    if result.returncode != 0:
        logger.debug("git log failed: %s", result.stderr.strip())
        return []
    commits: list[dict] = []
    for line in result.stdout.strip().split("\n"):
        if not line:
            continue
        parts = line.split("\x1f")
        if len(parts) < 4:
            continue
        commits.append(
            {
                "sha": parts[0],
                "author": parts[1],
                "date": parts[2],
                "subject": parts[3],
            }
        )
    return commits
