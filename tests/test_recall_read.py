"""Tests for ``theater.daemon.recall_read.read_segment``.

Each test is a genuine regression: it would fail if the behaviour it
covers were removed. The two paths that will actually happen in
production and are easiest to fake passing — a missing transcript and a
gap git cannot explain — each have a dedicated test.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from shipped import VibeHarness
from sqlalchemy import insert, select

from theater.daemon.recall_read import read_segment
from theater.daemon.schema import touch

# ---- helpers -------------------------------------------------------------


def _vibe_session(tmp_path: Path) -> tuple[Path, Path, str, Path]:
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


def _append(transcript: Path, *records: dict) -> None:
    with transcript.open("a") as fh:
        for record in records:
            fh.write(json.dumps(record) + "\n")
        fh.flush()


def _make_job(
    store,
    registry,
    *,
    handle: str,
    harness: str = "vibe",
    cwd: str | None = None,
    target_cwd: str | None = None,
    session_id: str | None = None,
) -> str:
    """Create a job row and its target participant. Returns the participant id."""
    p = registry.register(
        harness=harness,
        pane=None,
        cwd=target_cwd or cwd,
        session_id=session_id,
    )
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


def _add_touch(
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


def _init_git(repo: Path) -> None:
    """A real git repo with a configured author, for gap-segment tests."""
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=repo,
        check=True,
    )


def _commit(repo: Path, message: str) -> str:
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


def _blob_sha(repo: Path, ref: str) -> str:
    """The blob sha of a path at a git ref, via ``git rev-parse``."""
    result = subprocess.run(
        ["git", "rev-parse", ref],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


# ---- job segments --------------------------------------------------------


async def test_job_segment_returns_metadata_and_transcript(registry, tmp_path):
    """A job segment returns the job's database metadata and its transcript.

    Goes through ``open_source`` so an adapter whose output is a file
    (vibe) answers the same way one whose output is a database would.
    The transcript events are unclipped and filtered to readable kinds.
    """
    root, project, sid, transcript = _vibe_session(tmp_path)

    # Swap the shipped vibe harness for one pointed at our temp root,
    # so the observer finds our test transcript.
    from theater import harness as harness_mod

    vibe = VibeHarness(root=root)
    harness_mod.HARNESSES["vibe"] = vibe

    _append(
        transcript,
        {"role": "user", "content": "do the thing"},
        {"role": "assistant", "content": "it is done"},
    )

    _make_job(
        registry.store,
        registry,
        handle="vibe-abc",
        target_cwd=str(project),
        session_id=sid,
    )
    _add_touch(
        registry.store,
        handle="vibe-abc",
        path="src.py",
        sha_before="aaa",
        sha_after="bbb",
    )

    result = await read_segment(
        "vibe-abc", store=registry.store, registry=registry, cwd=str(tmp_path)
    )

    assert result["kind"] == "job"
    assert result["handle"] == "vibe-abc"
    assert result["task"] == "do the thing"
    assert result["result"] == "it is done"
    assert result["outcome"] == "done"
    assert result["harness"] == "vibe"
    assert result["session_id"] == sid
    assert len(result["paths"]) == 1
    assert result["paths"][0]["path"] == "src.py"
    assert result["paths"][0]["sha_before"] == "aaa"
    assert result["paths"][0]["sha_after"] == "bbb"
    # The transcript must be available and carry the events we wrote.
    assert result["transcript"]["available"] is True
    roles = [e["role"] for e in result["transcript"]["events"]]
    assert "user" in roles
    assert "assistant" in roles


async def test_job_segment_missing_transcript_still_returns_metadata(registry, tmp_path):
    """A job whose transcript no longer exists returns everything the
    database still remembers, with an explicit marker that the
    transcript is unavailable.

    This is the missing-transcript path from the design
    (docs/v2_recall.md line 538): 3 of 147 recorded transcript paths
    are gone from disk. It must NOT raise, and it must NOT return an
    empty dict.
    """
    # Point the vibe observer at a root that has no sessions at all,
    # so find_transcript returns None and history() comes back empty.
    from theater import harness as harness_mod

    empty_root = tmp_path / "empty_logs" / "session"
    empty_root.mkdir(parents=True)
    vibe = VibeHarness(root=empty_root)
    harness_mod.HARNESSES["vibe"] = vibe

    _make_job(
        registry.store,
        registry,
        handle="codex-dead",
        harness="vibe",
        target_cwd=str(tmp_path / "nowhere"),
    )
    _add_touch(
        registry.store,
        handle="codex-dead",
        path="gone.py",
        sha_before="ccc",
        sha_after=None,
    )

    result = await read_segment(
        "codex-dead",
        store=registry.store,
        registry=registry,
        cwd=str(tmp_path),
    )

    # Must not raise, must not be empty.
    assert result["kind"] == "job"
    assert result["handle"] == "codex-dead"
    assert result["task"] == "do the thing"
    assert result["outcome"] == "done"
    assert result["paths"][0]["path"] == "gone.py"
    assert result["paths"][0]["sha_after"] is None
    # The transcript is explicitly unavailable, with a reason.
    assert result["transcript"]["available"] is False
    assert "reason" in result["transcript"]
    assert "no longer" in result["transcript"]["reason"]


async def test_job_segment_with_no_target_returns_metadata_only(registry, tmp_path):
    """A job whose target_id is None (a CLI spawn with no target) has
    no transcript to read. The metadata is still there."""
    registry.store.create_job(
        type(
            "J",
            (),
            {
                "handle": "orphan",
                "caller_id": "caller",
                "target_id": None,
                "kind": "spawn",
                "prompt": "do something",
                "state": "done",
                "result": "ok",
                "error_code": None,
                "created_at": 1000.0,
                "finished_at": 1001.0,
            },
        )()
    )

    result = await read_segment(
        "orphan", store=registry.store, registry=registry, cwd=str(tmp_path)
    )

    assert result["kind"] == "job"
    assert result["handle"] == "orphan"
    assert result["transcript"]["available"] is False
    assert "no target" in result["transcript"]["reason"]


# ---- gap segments -------------------------------------------------------


async def test_gap_segment_git_can_explain(tmp_path):
    """A gap that git can explain returns the commits that touched the
    path between the two blob shas."""
    repo = tmp_path / "repo"
    _init_git(repo)

    # Create a file, commit it.
    f = repo / "src.py"
    f.write_text("original\n")
    _commit(repo, "initial")
    before_blob = _blob_sha(repo, "HEAD:src.py")

    # Modify the file, commit again.
    f.write_text("modified\n")
    _commit(repo, "change it")
    after_blob = _blob_sha(repo, "HEAD:src.py")

    segment = f"gap:src.py:{before_blob}..{after_blob}"
    result = await read_segment(segment, store=None, registry=None, cwd=str(repo))

    assert result["kind"] == "gap"
    assert result["path"] == "src.py"
    assert result["sha_before"] == before_blob
    assert result["sha_after"] == after_blob
    assert result["explained"] is True
    assert len(result["commits"]) >= 1
    commit = result["commits"][0]
    assert "sha" in commit
    assert "author" in commit
    assert "date" in commit
    assert "subject" in commit
    assert commit["subject"] == "change it"


async def test_gap_segment_git_cannot_explain(tmp_path):
    """A gap where the blobs are not in git history — an uncommitted
    local edit — says so explicitly rather than returning an empty list
    that reads like "nothing happened"."""
    repo = tmp_path / "repo"
    _init_git(repo)

    # Create and commit a file.
    f = repo / "src.py"
    f.write_text("original\n")
    _commit(repo, "initial")

    # Make an uncommitted edit. The blob sha of this content is not
    # in git history.
    f.write_text("uncommitted change\n")
    fake_before = "0" * 40
    fake_after = "1" * 40

    segment = f"gap:src.py:{fake_before}..{fake_after}"
    result = await read_segment(segment, store=None, registry=None, cwd=str(repo))

    assert result["kind"] == "gap"
    assert result["explained"] is False
    assert result["commits"] == []
    assert "note" in result
    assert "no commit" in result["note"]


async def test_gap_segment_refuses_path_escape(tmp_path):
    """A gap segment whose path contains ``..`` is an attack, not a
    typo. The git root is a hard privacy wall."""
    repo = tmp_path / "repo"
    _init_git(repo)

    segment = "gap:../../etc/passwd:aaa..bbb"
    result = await read_segment(segment, store=None, registry=None, cwd=str(repo))

    assert result["kind"] == "gap"
    assert result["explained"] is False
    assert result["commits"] == []
    assert "escapes" in result["note"]


async def test_gap_segment_not_in_a_git_repo(tmp_path):
    """A gap segment called from a directory that is not a git repo
    says so explicitly."""
    not_a_repo = tmp_path / "nope"
    not_a_repo.mkdir()

    segment = "gap:src.py:aaa..bbb"
    result = await read_segment(segment, store=None, registry=None, cwd=str(not_a_repo))

    assert result["kind"] == "gap"
    assert result["explained"] is False
    assert "not inside a git repository" in result["note"]


async def test_gap_segment_null_sha_sentinels(tmp_path):
    """A gap with ``-`` as a sha (null — a creation or deletion) is
    converted to None and does not crash."""
    repo = tmp_path / "repo"
    _init_git(repo)
    f = repo / "src.py"
    f.write_text("hello\n")
    _commit(repo, "create")
    blob = _blob_sha(repo, "HEAD:src.py")

    # ``-`` means the file did not exist before (creation).
    segment = f"gap:src.py:-..{blob}"
    result = await read_segment(segment, store=None, registry=None, cwd=str(repo))

    assert result["kind"] == "gap"
    assert result["sha_before"] is None
    assert result["sha_after"] == blob


# ---- read-only ----------------------------------------------------------


async def test_read_segment_writes_nothing_to_the_database(registry, tmp_path):
    """``read_segment`` is read-only with respect to the database. It
    writes nothing — not a log row, not a cache. Asserting the bus row
    count is unchanged before and after proves this."""
    from sqlalchemy import func

    from theater import harness as harness_mod
    from theater.daemon.schema import bus

    empty_root = tmp_path / "empty_logs" / "session"
    empty_root.mkdir(parents=True)
    harness_mod.HARNESSES["vibe"] = VibeHarness(root=empty_root)

    _make_job(
        registry.store,
        registry,
        handle="ro-test",
        target_cwd=str(tmp_path / "nowhere"),
    )

    # Measure after the job is created, so the bus rows that
    # ``registry.register`` writes do not count against ``read_segment``.
    before = registry.store.conn.execute(select(func.count()).select_from(bus)).scalar()

    # Read a job segment.
    await read_segment("ro-test", store=registry.store, registry=registry, cwd=str(tmp_path))
    # Read a gap segment.
    repo = tmp_path / "repo"
    _init_git(repo)
    await read_segment(
        "gap:src.py:aaa..bbb",
        store=registry.store,
        registry=registry,
        cwd=str(repo),
    )

    after = registry.store.conn.execute(select(func.count()).select_from(bus)).scalar()
    assert before == after
