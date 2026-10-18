"""A Vibe session, job and touch rows, and git helpers for segment-read tests."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from sqlalchemy import insert

from theater.daemon.schema import touch
from theater.provenance import TranscriptProvenance


def vibe_session(tmp_path: Path) -> tuple[Path, Path, str, Path]:
    """A Vibe log root with one session whose cwd is a temp project.

    Returns (root, project, session_id, transcript). The transcript is
    a real file that the VibeObserver can locate and parse, so the
    job-segment reader goes through the same ``open_source`` path as
    production.
    """
    root = tmp_path / "logs" / "session"
    session = root / "session_20260101_000000_deadbeef"
    session.mkdir(parents=True)
    project = tmp_path / "project"
    project.mkdir()
    sid = "deadbeef-1111-2222-3333"
    (session / "meta.json").write_text(
        json.dumps(
            {
                "session_id": sid,
                "environment": {"working_directory": str(project)},
            }
        )
    )
    transcript = session / "messages.jsonl"
    transcript.write_text("")
    return root, project, sid, transcript


def append_records(transcript: Path, *records: dict) -> None:
    with transcript.open("a") as fh:
        for record in records:
            fh.write(json.dumps(record) + "\n")
        fh.flush()


def make_job(
    store,
    registry,
    *,
    handle: str,
    harness: str = "vibe",
    cwd: str | None = None,
    target_cwd: str | None = None,
    session_id: str | None = None,
    session_correlation: str | None = None,
) -> str:
    """Create a job row and its target participant. Returns the participant id."""
    p = registry.register(
        harness=harness,
        pane=None,
        cwd=target_cwd or cwd,
        session_id=session_id,
    )
    if session_correlation is not None or session_id is not None:
        p.session_correlation = session_correlation or str(TranscriptProvenance.EXACT)
        registry.store.upsert_participant(p)
    store.create_job(
        type(
            "J",
            (),
            {
                "handle": handle,
                "caller_id": "caller",
                "target_id": p.id,
                "kind": "spawn",
                "prompt": "do the thing",
                "state": "done",
                "result": "it is done",
                "error_code": None,
                "created_at": 1000.0,
                "finished_at": 1001.0,
            },
        )()
    )
    return p.id


def add_touch(
    store,
    *,
    handle: str,
    path: str,
    sha_before: str | None,
    sha_after: str | None,
    mode: str = "write",
) -> None:
    store.conn.execute(
        insert(touch).values(
            job_handle=handle,
            path=path,
            mode=mode,
            sha_before=sha_before,
            sha_after=sha_after,
        )
    )


def init_git(repo: Path) -> None:
    """A real git repo with a configured author, for gap-segment tests."""
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=repo,
        check=True,
    )


def commit_all(repo: Path, message: str) -> str:
    """Stage everything and commit. Returns the commit sha."""
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", message], cwd=repo, check=True)
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def blob_sha_at(repo: Path, ref: str) -> str:
    """The blob sha of a path at a git ref, via ``git rev-parse``."""
    result = subprocess.run(
        ["git", "rev-parse", ref],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()
