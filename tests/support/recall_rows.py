"""Participant, job and touch rows and a scratch git repo for recall timelines."""

from __future__ import annotations

import subprocess

from theater.daemon.schema import touch
from theater.models import Job, JobState, Participant, Tier, now


def add_participant(
    store,
    *,
    pid: str = "p1",
    harness: str = "vibe",
    cwd: str = "/tmp/repo",
    branch: str | None = "main",
    session_id: str | None = "ses-123",
    parent_id: str | None = None,
) -> Participant:
    p = Participant(
        id=pid,
        harness=harness,
        tier=Tier.SPAWNED,
        cwd=cwd,
        branch=branch,
        session_id=session_id,
        parent_id=parent_id,
    )
    store.upsert_participant(p)
    return p


def add_job(
    store,
    *,
    handle: str,
    target_id: str,
    prompt: str = "do the thing",
    state: str = JobState.DONE,
    result: str | None = "done",
    finished_at: float | None = None,
    caller_id: str = "cli",
    response_format: str | None = None,
) -> None:
    store.create_job(
        Job(
            handle=handle,
            caller_id=caller_id,
            target_id=target_id,
            kind="spawn",
            prompt=prompt,
            response_format=response_format,
            state=state,
            result=result,
            error_code=None,
            created_at=now(),
            finished_at=finished_at if finished_at is not None else now(),
        )
    )


def add_touch_row(
    store,
    *,
    job_handle: str,
    path: str,
    mode: str = "write",
    sha_before: str | None = None,
    sha_after: str | None = None,
    sha_before_error: str | None = None,
    sha_after_error: str | None = None,
) -> None:
    store.conn.execute(
        touch.insert().values(
            job_handle=job_handle,
            path=path,
            mode=mode,
            sha_before=sha_before,
            sha_after=sha_after,
            sha_before_error=sha_before_error,
            sha_after_error=sha_after_error,
        )
    )


def setup_repo(tmp_path):
    """Create a git repo at tmp_path and return its root string.

    The root is resolved with ``Path.resolve()`` because macOS puts
    ``/tmp`` behind a symlink to ``/private/tmp``, and ``git
    rev-parse --show-toplevel`` returns the resolved path. If the
    participant cwd and the git root disagree on symlink resolution,
    the privacy wall's prefix match fails silently.
    """
    root = str(tmp_path.resolve())
    for cmd in (
        ["git", "init"],
        ["git", "config", "user.email", "t@t.com"],
        ["git", "config", "user.name", "test"],
    ):
        subprocess.run(cmd, cwd=root, capture_output=True, timeout=10, check=False)
    return root
