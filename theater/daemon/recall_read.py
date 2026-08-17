"""The segment reader for recall.

``recall`` returns a per-path timeline of points. Each point carries a
``segment`` id, and this module explains what happened inside one. There
are two kinds:

- **A job segment.** The id is a job handle verbatim (e.g. ``codex-a41f``).
  It contains no ``gap:`` prefix. The explanation is the job's metadata
  from the database plus its transcript, read back through the same
  ``open_source`` path that ``read_transcript`` uses
  (``methods.py:727``), so an adapter whose output is a database
  (opencode) answers as well as one that writes a file.

- **A gap segment.** The id is ``gap:<path>:<before>..<after>`` where
  ``before`` and ``after`` are git blob shas as stored, with a literal
  ``-`` where the sha was null. Nobody in Theater's records claims that
  transition, so this is the one place in the whole feature allowed to
  fork ``git log`` — to find which commits touched ``<path>`` and moved
  it between those two blob shas.

The function is read-only with respect to the database: it writes
nothing, not a log row, not a cache. Brief-derived text must not feed
back into the index as future evidence.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from sqlalchemy import select

from theater.daemon.observer import history_correlation_is_ambiguous
from theater.daemon.schema import jobs, touch
from theater.harness import HARNESSES, normalize
from theater.harness.observation import open_participant_source
from theater.models import BadRequest
from theater.provenance import TranscriptProvenance, is_trusted_provenance, normalize_provenance

logger = logging.getLogger("theater.recall.read")

#: Kinds reported in a job segment's transcript. ERROR is dropped: the
#: caller is an agent reading what was said, and a harness-level error
#: record is not part of the conversation. Same filter as
#: ``methods._READABLE``.
_READABLE = ("assistant", "user", "tool_call", "tool_result")

#: Ceiling on ``git log`` output for a gap segment. A path with ten
#: thousand commits must not hang the daemon; the caller gets a note that
#: the list was truncated and can narrow the query if they need more.
_MAX_GAP_COMMITS = 200

#: How long a ``git log`` for a gap segment may run before it is killed.
#: Two seconds is long enough for any real history lookup and short
#: enough that a pathological repo (huge pack, slow disk) does not stall
#: the daemon's event loop.
_GIT_TIMEOUT = 10


async def read_segment(
    segment_id: str,
    *,
    store,
    registry,
    cwd: str,
) -> dict:
    """Explain what happened inside one timeline segment.

    Takes explicit collaborators rather than a ``daemon`` object, so it
    is testable without standing up a daemon. ``store`` is
    ``theater.daemon.store.Store`` (read via ``store.conn``). ``registry``
    is ``theater.daemon.registry.Registry`` — ``registry.get(participant_id)``
    returns a participant or raises ``NotFound``. ``cwd`` is the caller's
    directory, which is what the git root is resolved from.

    Never raises for a segment that simply has no transcript or no
    git-explainable history. A caller asking about a real job that
    really happened deserves everything the database still remembers.
    """
    if segment_id.startswith("gap:"):
        return _read_gap(segment_id, cwd=cwd)
    return await _read_job(segment_id, store=store, registry=registry)


# ---- job segments --------------------------------------------------------


async def _read_job(
    handle: str,
    *,
    store,
    registry,
) -> dict:
    """The brief for a job segment: metadata from ``jobs`` plus the
    transcript read back through ``open_source``.

    Goes through ``harness.observer.open_source(...)`` rather than
    ``find_transcript``, for the reason ``_read_transcript``
    (``methods.py:727``) does: an adapter whose transcript is a database
    answers just as well as one that writes a file. The source opened
    here is short-lived and separate from the watcher's — reading
    history must not move the watcher's cursor — and is always closed in
    a ``finally``.
    """
    row = store.conn.execute(select(jobs).where(jobs.c.handle == handle)).first()
    if row is None:
        raise BadRequest(f"no job {handle!r}")
    j = row._mapping

    touch_rows = store.conn.execute(
        select(
            touch.c.path,
            touch.c.mode,
            touch.c.sha_before,
            touch.c.sha_after,
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
        }
        for r in touch_rows
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

    # A job whose target was None (a CLI spawn with no target) has no
    # transcript to read.
    target_id = j["target_id"]
    if target_id is None:
        brief["transcript"] = {
            "available": False,
            "reason": "job has no target participant",
        }
        return brief

    try:
        p = registry.get(target_id)
    except Exception:
        # The participant was forgotten — the job still happened; only
        # the transcript is unavailable.
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

    harness_name = normalize(p.harness)
    harness = HARNESSES.get(harness_name)
    if harness is None:
        # Harness adapter not loaded — the job metadata survives; only
        # the transcript is unreadable.
        brief["transcript"] = {
            "available": False,
            "reason": f"harness {p.harness!r} is not known",
        }
        return brief

    # Open a short-lived source separate from the watcher's; close in finally.
    source = open_participant_source(
        harness.observer,
        participant_id=p.id,
        cwd=p.cwd,
        session_id=p.session_id,
        after=None,
        session_exact=normalize_provenance(p.session_correlation) is TranscriptProvenance.EXACT,
        known_location=p.transcript_location,
        pane_pid=p.live_pid,
    )
    try:
        history = await source.history(last_n=0)
    except Exception:
        logger.debug("reading transcript for %s failed", handle, exc_info=True)
        brief["transcript"] = {
            "available": False,
            "reason": "transcript could not be read",
        }
        return brief
    finally:
        await source.aclose()

    if history.error_code is not None:
        brief["transcript"] = {
            "available": False,
            "reason": history.error or history.error_code,
            "error_code": history.error_code,
        }
        return brief

    if history.location is None:
        # The source located nothing — transcript file deleted, or the
        # opencode database has no session row.
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

    if history_correlation_is_ambiguous(registry, p.id, history):
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
        }
        for event in history.events
        if event.kind.value in _READABLE
    ]
    brief["transcript"] = {
        "available": True,
        "location": history.location,
        "events": events,
    }
    return brief


# ---- gap segments --------------------------------------------------------


def _read_gap(segment_id: str, *, cwd: str) -> dict:
    """The brief for a gap segment: which commits moved ``<path>`` from
    ``<before>`` to ``<after>``.

    This is the only place in the feature allowed to fork ``git log``.
    Everything else is pure SQL and hashing so that this one expensive
    call is spent deliberately, by a caller who has looked at a gap and
    decided they want to know.
    """
    # Parse ``gap:<path>:<before>..<after>``. Split from the right: the
    # path may contain colons (legal on Linux).
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

    # The git root is a hard privacy wall — never read outside the
    # caller's repo. ``..`` in a path is an attack, not a typo.
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

    # Refuse a path that escapes the root. ``realpath`` resolves ``..``
    # so the check is against the real filesystem, not a lexical one.
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
    """The toplevel of the git repo containing ``cwd``.

    Uses ``--git-common-dir`` rather than ``--show-toplevel`` so it
    resolves the main repo root from inside a linked worktree, where
    ``--show-toplevel`` would return the worktree's own top level. Same
    approach as ``worktree.main_repo_root`` (``worktree.py:90``).
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

    ``..`` in a path is treated as an escape attempt, not a typo: the
    privacy wall is hard. The check is against ``os.path.realpath`` so a
    symlink that points outside the repo is caught.
    """
    root_path = Path(root).resolve()
    # Reject ``..`` lexically, before resolution. Stricter than needed
    # but the cost of a false reject is low and a false accept is a
    # privacy breach.
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
    """Find commits that touched ``path`` and moved it between two shas.

    ``git log --find-object=<sha>`` finds commits that introduced or
    removed a specific blob — exactly the commits that moved ``path``
    from one content to another. When both shas are known, querying both
    and taking the intersection yields the commits that changed the blob
    from ``before`` to ``after``.

    When neither sha is in history (an uncommitted local edit, or blobs
    from outside this repo), ``--find-object`` returns nothing. That is
    the correct answer: no commit explains the transition. Falling back
    to the full path history would return every commit that ever touched
    the file, which is not what the caller asked and reads as "something
    happened" when the honest answer is "git cannot explain this".
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
    """One ``git log`` invocation, parsed into a list of commit dicts.

    The format is tab-separated (``%x1f`` is the unit separator): sha,
    author name, date, subject. No file contents, no diffs — index
    references and derived facts, never payloads.
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
