"""The recall query engine: per-file timelines, newest first, with gap points where hashes break.
SQL does joins, the privacy wall, and gaps; exactly two git forks per query. Moved-since-last-job is
answered by ``current`` vs the newest ``sha_after`` (blob hashes are not commit-ish).
"""

from __future__ import annotations

import datetime
import subprocess
from pathlib import Path
from typing import Any

from sqlalchemy import or_, select

from theater.constants.daemon import RECALL_HASH_MAX_QUERY_BYTES, TOUCH_HASH_MAX_FILE_BYTES
from theater.daemon.blob import BlobHash, BlobHashState, blob_hash
from theater.daemon.schema import jobs, participants, touch
from theater.daemon.store import Store
from theater.daemon.touch_paths import normalize_touch_path
from theater.harness import HARNESSES, supports_resume
from theater.harness import normalize as normalize_harness
from theater.models import BadRequest
from theater.provenance import is_trusted_provenance

#: Ceiling on ``task`` and ``result`` text in the timeline; full text lives behind ``recall_read``.
CLIP = 300

#: Default and maximum points per path timeline; counted after gaps are interleaved.
DEFAULT_DEPTH = 5


def _clip(text: str | None) -> str | None:
    """Clip to ``CLIP`` chars; ``None`` stays ``None`` so "no result" is not "said nothing"."""
    if text is None:
        return None
    return text[:CLIP]


def _sha_or_dash(sha: str | None) -> str:
    """Render a null sha as ``-`` so creation/deletion gap ids still parse as three fields."""
    return sha if sha is not None else "-"


def _sha_display(sha: str | None, error: str | None) -> str:
    return "?" if error is not None else _sha_or_dash(sha)


def _segment_id_for_gap(path: str, before: str | None, after: str | None) -> str:
    """The segment id for a gap point: ``gap:<path>:<before>..<after>``.

    A sibling agent parses this exact format; null shas render as ``-``.
    """
    return f"gap:{path}:{_sha_or_dash(before)}..{_sha_or_dash(after)}"


def _resume_info(
    harness_name: str,
    session_id: str | None,
    session_correlation: str | None,
) -> tuple[bool, str | None]:
    """Whether the caller can resume this session, and why not if not.

    Answered here because a spawn that fails after the participant exists leaves work behind.
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
    """``git rev-parse --show-toplevel`` — one fork; ``None`` outside a git repo."""
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
    """``git status --porcelain`` — one fork, the set of repo-relative dirty paths.

    A subprocess because gitignore and index semantics must not be reimplemented.
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
        # --porcelain: "XY path"; do NOT strip; renames show "XY  old -> new", take new path.
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
    current_hashes: dict[str, BlobHash],
) -> dict[str, dict]:
    """Query the touch table and build per-path timelines.
    The privacy wall is SQL: unattributable rows (no participant or cwd) are dropped. A gap is a
    ``sha_before`` differing from the previous row's ``sha_after``.
    """
    result: dict[str, dict] = {}

    for path in repo_paths:
        # Inner join on participants: rows we cannot attribute to a repo are excluded.
        stmt = (
            select(
                touch.c.job_handle,
                touch.c.path,
                touch.c.mode,
                touch.c.sha_before,
                touch.c.sha_after,
                touch.c.sha_before_error,
                touch.c.sha_after_error,
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
            # startswith(autoescape=True) not like(): LIKE treats _/% as wildcards.
            .where(
                or_(
                    participants.c.cwd == git_root,
                    participants.c.cwd.startswith(git_root.rstrip("/") + "/", autoescape=True),
                )
            )
            .order_by(jobs.c.finished_at.desc())
        )
        rows = store.conn.execute(stmt).fetchall()

        # Reads (sha_before == sha_after) are a count, not timeline points.
        writes = [
            r
            for r in rows
            if r.sha_before_error is not None
            or r.sha_after_error is not None
            or r.sha_before != r.sha_after
        ]
        reads = len(rows) - len(writes)

        # Gap detection: gap when sha_after != prev sha_before; _seen_prev needed for None.
        timeline: list[dict] = []
        prev_before: str | None = None
        prev_before_error: str | None = None
        _seen_prev = False
        for row in writes:
            if (
                _seen_prev
                and row.sha_after_error is None
                and prev_before_error is None
                and row.sha_after != prev_before
            ):
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
                "sha": (
                    f"{_sha_display(row.sha_before, row.sha_before_error)} → "
                    f"{_sha_display(row.sha_after, row.sha_after_error)}"
                ),
                "when": _format_ts(row.finished_at),
                "handle": row.job_handle,
                "harness": row.harness,
                "session_id": row.session_id,
                "resume": resume,
                "cwd": row.cwd,
                "branch": row.branch,
                # Lineage (parent) and provenance (caller) differ; bare ids keep parents private.
                "parent_id": row.parent_id,
                "caller_id": row.caller_id,
                "outcome": row.outcome,
                "task": _clip(row.prompt),
                "result": _clip(row.result),
            }
            if resume_note is not None:
                point["resume_note"] = resume_note
            if row.sha_before_error is not None:
                point["sha_before_error"] = row.sha_before_error
            if row.sha_after_error is not None:
                point["sha_after_error"] = row.sha_after_error
            timeline.append(point)
            if len(timeline) >= depth:
                break
            prev_before = row.sha_before
            prev_before_error = row.sha_before_error
            _seen_prev = True

        # ``dirty`` means working tree differs from HEAD; ``current`` vs sha_after detects drift.
        current_hash = current_hashes.get(path)
        if current_hash is None:
            current_hash = BlobHash(BlobHashState.UNAVAILABLE, reason="not_sampled")
        dirty = path in dirty_set

        result[path] = {
            "current": current_hash.digest,
            "current_status": str(current_hash.state),
            "dirty": dirty,
            "reads": reads,
            "timeline": timeline,
        }
        if current_hash.state is BlobHashState.UNAVAILABLE:
            result[path]["current_error"] = current_hash.reason

    return result


def _format_ts(finished_at: float | None) -> str | None:
    """Render an epoch as ISO-8601 Z; ``None`` means never finished, distinct from unknown."""
    if finished_at is None:
        return None

    return datetime.datetime.fromtimestamp(finished_at, tz=datetime.UTC).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _normalise_paths(paths: list[str], git_root: str) -> list[str]:
    """Return canonical paths contained by ``git_root``."""
    root = Path(git_root).resolve(strict=False)
    out: list[str] = []
    for raw in paths:
        if not isinstance(raw, str) or not raw:
            raise ValueError("recall paths must be non-empty strings")
        candidate = Path(raw)
        if candidate.is_absolute():
            try:
                relative = str(candidate.relative_to(root))
            except ValueError as exc:
                raise ValueError(f"recall path is outside the repository: {raw!r}") from exc
        else:
            relative = raw
        normalized = normalize_touch_path(root, relative)
        if normalized is None:
            raise ValueError(f"recall path is outside the repository: {raw!r}")
        if normalized not in out:
            out.append(normalized)
    return out


#: Sentinel for precomputed_root — distinguishes "not provided" from "provided but None".
_UNSET: Any = object()


def recall(
    store: Store,
    *,
    paths: list[str],
    depth: int = DEFAULT_DEPTH,
    caller_cwd: str | None = None,
    precomputed_root: Any | str | None = _UNSET,
    precomputed_dirty: set[str] | None = None,
    precomputed_current: dict[str, BlobHash] | None = None,
) -> dict[str, dict]:
    """Build per-file timelines from the touch table; untouched paths get empty timelines.
    Two forks per query regardless of path count (per-path forks measured 985 ms / 43 files). The
    ``precomputed_root`` sentinel keeps a legitimate ``None`` from forking git on the event loop.
    """
    if not paths:
        return {}

    cwd = caller_cwd or str(Path.cwd())
    root = precomputed_root if precomputed_root is not _UNSET else _git_root(cwd)
    if root is None:
        # Not a git repo: no dirty set, no root to normalise against — degraded but not broken.
        root = cwd

    repo_paths = _normalise_paths(paths, root)

    dirty = precomputed_dirty if precomputed_dirty is not None else _dirty_set(cwd)
    current = (
        precomputed_current
        if precomputed_current is not None
        else hash_current_files(root, repo_paths)
    )

    return _build_timeline(
        store,
        repo_paths=repo_paths,
        git_root=root,
        depth=depth,
        dirty_set=dirty,
        current_hashes=current,
    )


def hash_current_files(git_root: str, paths: list[str]) -> dict[str, BlobHash]:
    """Hash requested paths for ``recall`` without accessing daemon-owned state."""
    remaining = RECALL_HASH_MAX_QUERY_BYTES
    result: dict[str, BlobHash] = {}
    for path in _normalise_paths(paths, git_root):
        allowance = min(TOUCH_HASH_MAX_FILE_BYTES, remaining)
        outcome = blob_hash(
            Path(git_root) / path,
            max_bytes=allowance,
        )
        result[path] = outcome
        if outcome.state is BlobHashState.HASHED:
            remaining -= outcome.size
        elif outcome.reason in {"changed_while_reading", "path_changed", "read_failed"}:
            remaining -= allowance
    return result


async def recall_query(
    store: Store,
    *,
    paths: list[str],
    depth: int = DEFAULT_DEPTH,
    caller_cwd: str | None = None,
) -> dict[str, dict]:
    """Prepare bounded filesystem facts off-loop, then read the recall timeline."""
    from theater.daemon import workers

    if not paths:
        raise BadRequest("recall paths must be a non-empty list")
    effective_cwd = caller_cwd or str(Path.cwd())
    root = await workers.to_thread(_git_root, effective_cwd, label="recall.git_root")
    dirty = await workers.to_thread(_dirty_set, effective_cwd, label="recall.dirty_set")
    try:
        current = await workers.to_thread(
            hash_current_files,
            root or effective_cwd,
            paths,
            label="recall.current_hashes",
        )
    except ValueError as exc:
        raise BadRequest(str(exc)) from exc
    return recall(
        store,
        paths=paths,
        depth=depth,
        caller_cwd=caller_cwd,
        precomputed_root=root,
        precomputed_dirty=dirty,
        precomputed_current=current,
    )
