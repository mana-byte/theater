"""The recall query engine: per-file timelines from the touch table.

A timeline is a path's history of job touches, newest first, interleaved
with gap points where the hash chain breaks. Each job point carries
enough to resume the session that made it; each gap point carries enough
to decide whether to spend a ``recall_read`` explaining it.

The design has three budgets. SQL does everything that can be done in
SQL: the join, the privacy wall, the gap detection. ``blob_sha`` does
the hashing without forking git. Exactly two subprocess calls per
query — ``rev-parse`` and ``status`` — cover the live-git questions
that must not be reimplemented. See ``docs/v2_recall.md`` Piece 3.

The doc's third call, ``git diff --name-only <oldest_head>..HEAD`` for
a committed-change set, is not implemented and cannot be: ``touch``
records blob hashes, and a blob hash is not commit-ish, so the diff
exits on its usage message. Nothing is lost. The question that set was
meant to answer — has this file moved since the last job left it — is
answered for free by comparing ``current`` against the newest point's
``sha_after``, which catches committed and uncommitted changes alike
at the cost of zero forks. Attributing a committed change to its
commit would need a ``head_commit`` column on ``touch``; that is a
schema change, not a query change.
"""

from __future__ import annotations

import datetime
import subprocess
from pathlib import Path
from typing import Any

from sqlalchemy import or_, select

from theater.daemon.blob import blob_sha
from theater.daemon.schema import jobs, participants, touch
from theater.daemon.store import Store
from theater.harness import HARNESSES, supports_resume
from theater.harness import normalize as normalize_harness
from theater.provenance import is_trusted_provenance

#: Ceiling on ``task`` and ``result`` text in the timeline. Full text lives
#: behind ``recall_read``, which the sibling agent owns.
CLIP = 300

#: Default and maximum points per path timeline. Counted after gaps are
#: interleaved, so a depth of 5 means five points total — not five jobs
#: plus the gaps between them.
DEFAULT_DEPTH = 5


def _clip(text: str | None) -> str | None:
    """Clip to ``CLIP`` chars, preserving the first ``CLIP`` of the text.

    ``None`` stays ``None``: a crashed job has no result, and converting
    that to an empty string would read as "the job said nothing" rather
    than "the job never produced a result".
    """
    if text is None:
        return None
    return text[:CLIP]


def _sha_or_dash(sha: str | None) -> str:
    """Render a sha as ``-`` when null, for the gap segment id format.

    A null ``sha_before`` means the file was created; a null ``sha_after``
    means it was deleted. The segment id uses ``-`` so a gap spanning a
    creation or deletion still parses as three ``:`` -delimited fields.
    """
    return sha if sha is not None else "-"


def _segment_id_for_gap(path: str, before: str | None, after: str | None) -> str:
    """The segment id for a gap point: ``gap:<path>:<before>..<after>``.

    A sibling agent parses this exact format. Shas use ``-`` for null,
    matching ``_sha_or_dash``, so a gap at a creation or deletion is
    still three colon-delimited fields.
    """
    return f"gap:{path}:{_sha_or_dash(before)}..{_sha_or_dash(after)}"


def _resume_info(
    harness_name: str,
    session_id: str | None,
    session_correlation: str | None,
) -> tuple[bool, str | None]:
    """Whether the caller can resume this session, and why not if not.

    ``resume: true`` only when the harness adapter accepts a ``resume``
    parameter in ``plan_launch`` AND a session id was actually recorded.
    The caller must learn this here rather than discovering it at spawn
    time — a spawn that fails after the participant exists leaves work
    behind. See ``docs/v2_recall.md`` §"resume is a capability".
    """
    harness = HARNESSES.get(normalize_harness(harness_name))
    if harness is None:
        return False, "harness not registered"
    if not supports_resume(harness):
        return False, f"harness {harness_name!r} does not support resume"
    if not session_id:
        return False, "no session id recorded"
    if not is_trusted_provenance(session_correlation):
        return (
            False,
            "session id was found only by cwd/time; wait for exact/proven correlation "
            "or bind it before resuming",
        )
    return True, None


def _git_root(cwd: str) -> str | None:
    """``git rev-parse --show-toplevel`` — one fork, finds the repo root.

    Returns ``None`` if ``cwd`` is not inside a git repo. A participant
    whose cwd is not under git has no dirty set and no diff to compute,
    so the caller simply sees ``current`` from ``blob_sha`` and no
    dirty flag.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _dirty_set(cwd: str) -> set[str]:
    """``git status --porcelain`` — one fork, the set of dirty paths.

    Repo-relative paths, because that is how ``touch.path`` is stored.
    A path is dirty when the working tree differs from HEAD —
    uncommitted edits, untracked files, staged changes all qualify.
    This is the one place we depend on gitignore rules and index state,
    which is why it stays a subprocess rather than being reimplemented.
    """
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return set()
    if result.returncode != 0:
        return set()
    paths: set[str] = set()
    for line in result.stdout.splitlines():
        if not line:
            continue
        # --porcelain: "XY path", XY = two status chars then a space.
        # Do NOT strip — the leading char may itself be a space.
        # Renames show "XY  old -> new"; take the new path.
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        paths.add(path)
    return paths


def _build_timeline(
    store: Store,
    *,
    repo_paths: list[str],
    git_root: str,
    depth: int,
    dirty_set: set[str],
) -> dict[str, dict]:
    """Query the touch table and build per-path timelines.

    The privacy wall is in SQL: ``participants.cwd`` must start with the
    caller's git root, so a job from another repo is excluded before it
    reaches Python. A job whose participant row is gone, or whose cwd is
    null, is excluded — a row you cannot attribute to a repo is a row
    you cannot safely return.

    Gap detection is pure SQL-shaped: rows come back ordered by
    ``finished_at`` descending per path, and each row's ``sha_before``
    is compared against the previous row's ``sha_after``. A mismatch
    means something changed the file that no job claims.
    """
    result: dict[str, dict] = {}

    for path in repo_paths:
        # Inner join on participants: rows we cannot attribute to a repo
        # are excluded, which is the correct failure mode.
        stmt = (
            select(
                touch.c.job_handle,
                touch.c.path,
                touch.c.mode,
                touch.c.sha_before,
                touch.c.sha_after,
                jobs.c.state.label("outcome"),
                jobs.c.prompt,
                jobs.c.result,
                jobs.c.finished_at,
                participants.c.harness,
                participants.c.session_id,
                participants.c.session_correlation,
                participants.c.cwd,
                participants.c.branch,
                participants.c.parent_id,
                jobs.c.caller_id,
            )
            .select_from(
                touch.join(jobs, touch.c.job_handle == jobs.c.handle).join(
                    participants, jobs.c.target_id == participants.c.id
                )
            )
            .where(touch.c.path == path)
            # startswith(autoescape=True), not like(f"{root}%"): LIKE
            # treats `_` and `%` as wildcards, both legal in a directory
            # name — a privacy wall that widens on punctuation is no wall.
            # The boundary is enforced too: exact root equality OR a prefix
            # that ends at a path separator, so ``/work/repo`` does not
            # match a sibling at ``/work/repo-secret``.
            .where(
                or_(
                    participants.c.cwd == git_root,
                    participants.c.cwd.startswith(git_root.rstrip("/") + "/", autoescape=True),
                )
            )
            .order_by(jobs.c.finished_at.desc())
        )
        rows = store.conn.execute(stmt).fetchall()

        # Reads (sha_before == sha_after) are a count, not timeline
        # points — rendering them as points buries the writes.
        writes = [r for r in rows if r.sha_before != r.sha_after]
        reads = len(rows) - len(writes)

        # Gap detection in descending order: a gap exists when this
        # row's sha_after does not match the sha_before of the row
        # above (newer). ``_seen_prev`` is needed because
        # ``prev_before`` can be None legitimately (a creation), and
        # a ``is not None`` guard would suppress a real gap there.
        timeline: list[dict] = []
        prev_before: str | None = None
        _seen_prev = False
        for row in writes:
            if _seen_prev and row.sha_after != prev_before:
                timeline.append(
                    {
                        "gap": True,
                        "segment": _segment_id_for_gap(path, row.sha_after, prev_before),
                        "sha": f"{_sha_or_dash(row.sha_after)} → {_sha_or_dash(prev_before)}",
                        "note": "no job claims this transition",
                    }
                )
                if len(timeline) >= depth:
                    break

            resume, resume_note = _resume_info(
                row.harness,
                row.session_id,
                row.session_correlation,
            )
            point: dict = {
                "segment": row.job_handle,
                "sha": f"{_sha_or_dash(row.sha_before)} → {_sha_or_dash(row.sha_after)}",
                "when": _format_ts(row.finished_at),
                "handle": row.job_handle,
                "harness": row.harness,
                "session_id": row.session_id,
                "resume": resume,
                "cwd": row.cwd,
                "branch": row.branch,
                # Lineage (who spawned the editor, None for a root) and
                # provenance (who ordered this job — a `send` caller is
                # often a sibling) are different questions. Bare ids: a
                # parent can sit outside the caller's repo, so its cwd
                # and session id stay behind the privacy wall above.
                "parent_id": row.parent_id,
                "caller_id": row.caller_id,
                "outcome": row.outcome,
                "task": _clip(row.prompt),
                "result": _clip(row.result),
            }
            if resume_note is not None:
                point["resume_note"] = resume_note
            timeline.append(point)
            if len(timeline) >= depth:
                break
            prev_before = row.sha_before
            _seen_prev = True

        # ``dirty`` means the working tree differs from HEAD, not
        # "differs from where the last job left it" — that is
        # ``current`` against the newest point's sha_after, which the
        # caller reads off the timeline without us collapsing two
        # facts into one flag.
        abs_path = Path(git_root) / path
        current = blob_sha(abs_path)
        dirty = path in dirty_set

        result[path] = {
            "current": current,
            "dirty": dirty,
            "reads": reads,
            "timeline": timeline,
        }

    return result


def _format_ts(finished_at: float | None) -> str | None:
    """Render a Unix epoch as ISO-8601 Z, or ``None`` if the job never finished.

    A job that is still running has no ``finished_at``; a crashed job may
    have one set by the observer's rescue path. ``None`` rather than an
    empty string so the caller can distinguish "never finished" from
    "finished at an unknown time".
    """
    if finished_at is None:
        return None

    return datetime.datetime.fromtimestamp(finished_at, tz=datetime.UTC).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _normalise_paths(paths: list[str], git_root: str) -> list[str]:
    """Convert absolute or repo-relative paths to repo-relative.

    ``touch.path`` is always repo-relative, so the query must match that
    form. Absolute paths are stripped of the git root prefix; paths that
    do not start with the root are passed through (they may be relative
    already, or from a different repo, in which case the query returns
    an empty timeline — which is the correct answer).
    """
    root = git_root.rstrip("/") + "/"
    out = []
    for p in paths:
        if p.startswith(root):
            out.append(p[len(root) :])
        elif p.startswith(git_root + "/"):
            out.append(p[len(git_root) + 1 :])
        else:
            out.append(p)
    return out


#: Sentinel for ``precomputed_root`` — distinguishes "not provided" from
#: "provided but ``None``" (caller's cwd is not a git repo).  Without this,
#: a legitimately-``None`` precomputed root would make ``recall()`` re-call
#: ``_git_root`` synchronously on the event loop — exactly the blocking
#: pattern this migration set out to eliminate.
_UNSET: Any = object()


def recall(
    store: Store,
    *,
    paths: list[str],
    depth: int = DEFAULT_DEPTH,
    caller_cwd: str | None = None,
    precomputed_root: Any | str | None = _UNSET,
    precomputed_dirty: set[str] | None = None,
) -> dict[str, dict]:
    """Build per-file timelines from the touch table.

    Returns one ``PathTimeline`` per path, keyed by the repo-relative
    path as stored. A path that has never been touched comes back as an
    empty timeline — not an error and not a missing key — because the
    caller asked about it and an answer about it is better than silence.

    Two subprocess calls per query, regardless of how many paths were
    asked for: one ``rev-parse`` to find the root, one ``status`` for
    the dirty set. The naive shape — one fork per path — was measured
    at 985 ms across 43 files and is forbidden. See
    ``docs/v2_recall.md`` §"The git budget", and the module docstring
    for why the doc's third call is absent.

    ``caller_cwd`` is where the caller's git root is found. It defaults
    to the process cwd, which is correct for the MCP tool (an agent's
    cwd is its repo root) and for direct calls in tests.

    ``precomputed_root`` and ``precomputed_dirty`` let an async caller
    that has already offloaded the git calls (via ``workers.to_thread``)
    pass in the results so the sync body forks nothing. When ``None``
    (the default for ``precomputed_dirty``), the function computes them
    inline — today's exact behavior, so all existing tests are unchanged.
    ``precomputed_root`` uses a sentinel default so that a legitimately
    ``None`` result (cwd is not a git repo) is distinguished from "not
    provided" — without this, a ``None`` precomputed root would trigger
    a synchronous ``_git_root`` call back on the event loop.
    """
    if not paths:
        return {}

    cwd = caller_cwd or str(Path.cwd())
    root = precomputed_root if precomputed_root is not _UNSET else _git_root(cwd)
    if root is None:
        # Not a git repo: no dirty set, no root to normalise against.
        # Degraded but not broken.
        root = cwd

    repo_paths = _normalise_paths(paths, root)

    dirty = precomputed_dirty if precomputed_dirty is not None else _dirty_set(cwd)

    return _build_timeline(
        store,
        repo_paths=repo_paths,
        git_root=root,
        depth=depth,
        dirty_set=dirty,
    )
