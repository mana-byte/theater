"""Regression tests for the recall query engine.

Each test covers a load-bearing behaviour from ``docs/v2_recall.md``
Piece 3. Removing the behaviour it guards must make the test fail —
a test that asserts a mock returns what it was told proves nothing.

The git budget test (``test_fork_count_does_not_scale_with_paths``)
is the point of the whole design: without it nothing stops a later
refactor from reintroducing the per-path fork that was measured at
985 ms across 43 files.
"""

from __future__ import annotations

import subprocess
from unittest.mock import patch

from theater.daemon.recall import (
    CLIP,
    _git_root,
    recall,
)
from theater.daemon.schema import touch
from theater.models import Job, JobState, Participant, Tier, now

# ---- helpers ---------------------------------------------------------------


def _participant(
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


def _job(
    store,
    *,
    handle: str,
    target_id: str,
    prompt: str = "do the thing",
    state: str = JobState.DONE,
    result: str | None = "done",
    finished_at: float | None = None,
    caller_id: str = "cli",
) -> None:
    store.create_job(
        Job(
            handle=handle,
            caller_id=caller_id,
            target_id=target_id,
            kind="spawn",
            prompt=prompt,
            state=state,
            result=result,
            error_code=None,
            created_at=now(),
            finished_at=finished_at if finished_at is not None else now(),
        )
    )


def _touch(
    store,
    *,
    job_handle: str,
    path: str,
    mode: str = "write",
    sha_before: str | None = None,
    sha_after: str | None = None,
) -> None:
    store.conn.execute(
        touch.insert().values(
            job_handle=job_handle,
            path=path,
            mode=mode,
            sha_before=sha_before,
            sha_after=sha_after,
        )
    )


def _setup_repo(tmp_path):
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


# ---- the git budget: fork count is constant regardless of path count -------


def test_fork_count_does_not_scale_with_paths(store, tmp_path):
    """Three forks per call, regardless of how many paths were asked for.

    The naive shape — one subprocess per candidate path — was measured
    at 985 ms across 43 files. This test is the guardrail: it counts
    subprocess invocations and asserts the count is the same whether
    the caller asks about 1 path or 20. If a refactor reintroduces the
    per-path fork, this test fails.

    The count is 2: one ``rev-parse``, one ``status``. The doc's third
    call, a ``git diff`` for committed changes, is absent on purpose —
    see the module docstring in ``theater/daemon/recall.py``.
    """
    root = _setup_repo(tmp_path)

    # Seed touch rows for 20 paths so the query has something to chew on.
    p = _participant(store, cwd=root)
    _job(store, handle="h1", target_id=p.id)
    for i in range(20):
        _touch(
            store,
            job_handle="h1",
            path=f"file_{i}.py",
            sha_before=f"sha_b_{i}",
            sha_after=f"sha_a_{i}",
        )

    counts = []

    def _count(*args, **kwargs):
        counts.append(args[0])
        # Distinguish the three git commands by their argv.
        argv = args[0]
        if "rev-parse" in argv:
            stdout = root + "\n"
        else:
            stdout = "\n".join(f"file_{i}.py" for i in range(20)) + "\n"
        return subprocess.CompletedProcess(
            args=argv,
            returncode=0,
            stdout=stdout,
            stderr="",
        )

    with patch("theater.daemon.recall.subprocess.run", side_effect=_count):
        recall(store, paths=["file_0.py"], caller_cwd=root)
        one_path_count = len(counts)

    counts.clear()

    with patch("theater.daemon.recall.subprocess.run", side_effect=_count):
        recall(
            store,
            paths=[f"file_{i}.py" for i in range(20)],
            caller_cwd=root,
        )
        twenty_path_count = len(counts)

    assert one_path_count == twenty_path_count, (
        f"fork count scaled from {one_path_count} to {twenty_path_count} "
        f"when going from 1 to 20 paths — the per-path fork is back"
    )
    assert one_path_count == 2, (
        f"expected exactly 2 forks (rev-parse, status), got {one_path_count}"
    )


# ---- a crashed job still appears in the timeline ---------------------------


def test_crashed_job_appears_in_timeline(store, tmp_path):
    """A crashed job wrote to the file and its sha is in the chain.

    Outcome sorts a job down, it never filters it out. Somebody has to
    know an incomplete edit landed — that is the whole point of not
    silently dropping it.
    """
    root = _setup_repo(tmp_path)
    p = _participant(store, cwd=root, session_id="ses-crash")
    _job(
        store,
        handle="crashed-job",
        target_id=p.id,
        state=JobState.CRASHED,
        result=None,
    )
    _touch(
        store,
        job_handle="crashed-job",
        path="main.py",
        sha_before="aaa",
        sha_after="bbb",
    )

    result = recall(store, paths=["main.py"], caller_cwd=root)
    timeline = result["main.py"]["timeline"]
    assert len(timeline) == 1
    point = timeline[0]
    assert point["outcome"] == "crashed"
    assert point["result"] is None
    assert point["handle"] == "crashed-job"
    assert point["session_id"] == "ses-crash"


# ---- gap detection ---------------------------------------------------------


def test_gap_emitted_when_chain_breaks(store, tmp_path):
    """A mismatch between sha_after and the next sha_before is a gap.

    Job A left the file at ``bbb``; job B found it at ``ccc``. Nothing
    in touch explains the transition. A gap point must appear between
    them, with the right segment id format.
    """
    root = _setup_repo(tmp_path)
    p = _participant(store, cwd=root)

    # Older job (finished first).
    _job(store, handle="older", target_id=p.id, finished_at=1000.0)
    _touch(
        store,
        job_handle="older",
        path="f.py",
        sha_before="aaa",
        sha_after="bbb",
    )

    # Newer job (finished second, so it sorts above).
    _job(store, handle="newer", target_id=p.id, finished_at=2000.0)
    _touch(
        store,
        job_handle="newer",
        path="f.py",
        sha_before="ccc",  # does not match older's sha_after of "bbb"
        sha_after="ddd",
    )

    result = recall(store, paths=["f.py"], caller_cwd=root)
    timeline = result["f.py"]["timeline"]

    # Newest first: newer job, then gap, then older job.
    assert len(timeline) == 3
    assert timeline[0]["handle"] == "newer"
    assert timeline[1]["gap"] is True
    assert timeline[1]["segment"] == "gap:f.py:bbb..ccc"
    assert timeline[1]["sha"] == "bbb → ccc"
    assert timeline[2]["handle"] == "older"


def test_no_gap_when_chain_is_intact(store, tmp_path):
    """Matching sha_after → sha_before produces no gap point."""
    root = _setup_repo(tmp_path)
    p = _participant(store, cwd=root)

    _job(store, handle="first", target_id=p.id, finished_at=1000.0)
    _touch(
        store,
        job_handle="first",
        path="f.py",
        sha_before="aaa",
        sha_after="bbb",
    )

    _job(store, handle="second", target_id=p.id, finished_at=2000.0)
    _touch(
        store,
        job_handle="second",
        path="f.py",
        sha_before="bbb",  # matches first's sha_after — no gap
        sha_after="ccc",
    )

    result = recall(store, paths=["f.py"], caller_cwd=root)
    timeline = result["f.py"]["timeline"]
    assert len(timeline) == 2
    assert not any("gap" in pt for pt in timeline)


def test_gap_segment_id_uses_dash_for_null(store, tmp_path):
    """A null sha in a gap uses ``-``, not the string ``None``.

    A gap at a creation (null sha_before) or deletion (null sha_after)
    must still produce a three-field segment id that the sibling's
    parser can split on colons.
    """
    root = _setup_repo(tmp_path)
    p = _participant(store, cwd=root)

    # Job A leaves sha_after="aaa" (file exists at sha aaa).
    _job(store, handle="first", target_id=p.id, finished_at=1000.0)
    _touch(
        store,
        job_handle="first",
        path="gone.py",
        sha_before="bbb",
        sha_after="aaa",
    )

    # Job B finds sha_before=None (file did not exist). Gap: something
    # deleted the file between A's sha_after and B's sha_before.
    _job(store, handle="second", target_id=p.id, finished_at=2000.0)
    _touch(
        store,
        job_handle="second",
        path="gone.py",
        sha_before=None,
        sha_after="ccc",
    )

    result = recall(store, paths=["gone.py"], caller_cwd=root)
    timeline = result["gone.py"]["timeline"]

    # Newest first: second, gap, first.
    gap = timeline[1]
    assert gap["gap"] is True
    # first's sha_after was "aaa", second's sha_before was None → "-"
    assert gap["segment"] == "gap:gone.py:aaa..-"
    assert gap["sha"] == "aaa → -"


# ---- the privacy wall ------------------------------------------------------


def test_jobs_from_other_repo_are_excluded(store, tmp_path):
    """A participant whose cwd is in a different repo is filtered out.

    ``theater/daemon/store.py`` in two different repos is the same
    repo-relative string and would collide. The privacy wall filters
    every query to the caller's repo by prefix-matching the absolute
    participant cwd against the caller's git root.
    """
    repo_a = tmp_path / "repo_a"
    repo_b = tmp_path / "repo_b"
    repo_a.mkdir()
    repo_b.mkdir()
    root_a = _setup_repo(repo_a)
    root_b = _setup_repo(repo_b)

    # Participant in repo B touches the same path.
    p_b = _participant(store, pid="p-b", cwd=root_b)
    _job(store, handle="h-b", target_id=p_b.id)
    _touch(
        store,
        job_handle="h-b",
        path="shared.py",
        sha_before="aaa",
        sha_after="bbb",
    )

    # Participant in repo A touches the same path.
    p_a = _participant(store, pid="p-a", cwd=root_a)
    _job(store, handle="h-a", target_id=p_a.id)
    _touch(
        store,
        job_handle="h-a",
        path="shared.py",
        sha_before="ccc",
        sha_after="ddd",
    )

    # Query from repo A — only repo A's job should appear.
    result = recall(store, paths=["shared.py"], caller_cwd=root_a)
    timeline = result["shared.py"]["timeline"]
    assert len(timeline) == 1
    assert timeline[0]["handle"] == "h-a"

    # Query from repo B — only repo B's job should appear.
    result = recall(store, paths=["shared.py"], caller_cwd=root_b)
    timeline = result["shared.py"]["timeline"]
    assert len(timeline) == 1
    assert timeline[0]["handle"] == "h-b"


def test_job_with_null_cwd_is_excluded(store, tmp_path):
    """A participant whose cwd is null cannot be attributed to a repo.

    A row you cannot attribute to a repo is a row you cannot safely
    return. The inner join on participants excludes it.
    """
    root = _setup_repo(tmp_path)

    # Participant with null cwd.
    p_null = Participant(
        id="p-null",
        harness="vibe",
        tier=Tier.SPAWNED,
        cwd=None,
    )
    store.upsert_participant(p_null)
    _job(store, handle="h-null", target_id="p-null")
    _touch(
        store,
        job_handle="h-null",
        path="x.py",
        sha_before="aaa",
        sha_after="bbb",
    )

    # Participant in the real repo.
    p = _participant(store, pid="p-real", cwd=root)
    _job(store, handle="h-real", target_id=p.id)
    _touch(
        store,
        job_handle="h-real",
        path="x.py",
        sha_before="ccc",
        sha_after="ddd",
    )

    result = recall(store, paths=["x.py"], caller_cwd=root)
    timeline = result["x.py"]["timeline"]
    assert len(timeline) == 1
    assert timeline[0]["handle"] == "h-real"


def test_underscore_in_root_is_not_a_sql_wildcard(store, tmp_path):
    """An underscore is legal in a directory name and a wildcard in LIKE.

    The wall was first written as ``cwd.like(f"{root}%")``, which reads
    ``_`` as "any single character": a caller in ``my_repo`` would be
    handed the touch rows of a sibling ``myXrepo``. Nobody would name a
    directory to exploit that, but underscores in repository names are
    ordinary, and a wall that widens on ordinary punctuation is not a
    wall. ``startswith(..., autoescape=True)`` escapes the metacharacters.
    """
    mine = tmp_path / "my_repo"
    mine.mkdir()
    root = _setup_repo(mine)

    # Never created on disk — it only has to exist as a cwd string in
    # the participants table for the wall to have something to leak.
    sibling = str((tmp_path / "myXrepo").resolve())
    p = _participant(store, pid="p-sibling", cwd=sibling)
    _job(store, handle="h-sibling", target_id=p.id)
    _touch(
        store,
        job_handle="h-sibling",
        path="secret.py",
        sha_before="aaa",
        sha_after="bbb",
    )

    result = recall(store, paths=["secret.py"], caller_cwd=root)
    assert result["secret.py"]["timeline"] == [], (
        "a sibling repo's touch rows came back through the privacy wall"
    )


def test_worktree_child_is_visible_to_parent(store, tmp_path):
    """A managed worktree child at ``<root>/.theater/worktrees/<id>`` is a
    descendant of the repo root and must remain visible to the parent.

    This is the primary use case of recall: a parent recalling its own
    worktree child's work. The boundary fix must not exclude descendants.
    """
    root = _setup_repo(tmp_path)
    child_cwd = f"{root}/.theater/worktrees/child-abc"
    p = _participant(store, pid="p-child", cwd=child_cwd, session_id="ses-child")
    _job(
        store,
        handle="h-child",
        target_id=p.id,
        prompt="fix the bug",
        result="all good",
    )
    _touch(
        store,
        job_handle="h-child",
        path="main.py",
        sha_before="aaa",
        sha_after="bbb",
    )

    result = recall(store, paths=["main.py"], caller_cwd=root)
    timeline = result["main.py"]["timeline"]
    assert len(timeline) == 1
    assert timeline[0]["handle"] == "h-child"
    assert timeline[0]["task"] == "fix the bug"
    assert timeline[0]["result"] == "all good"


def test_prefix_named_sibling_repo_is_excluded(store, tmp_path):
    """A participant in a prefix-named sibling repo must not leak.

    root ``/tmp/xxx/repo`` vs participant cwd ``/tmp/xxx/repo-secret``:
    the old bare ``startswith`` matched because ``repo`` is a prefix of
    ``repo-secret``. The boundary fix requires exact equality or a
    separator after the root, so the sibling's prompt and result must
    not appear in the output.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    root = _setup_repo(repo)

    sibling_cwd = str((tmp_path / "repo-secret").resolve())
    p = _participant(
        store,
        pid="p-sibling",
        cwd=sibling_cwd,
        session_id="ses-secret",
    )
    _job(
        store,
        handle="h-secret",
        target_id=p.id,
        prompt="top secret prompt",
        result="top secret result",
    )
    _touch(
        store,
        job_handle="h-secret",
        path="shared.py",
        sha_before="aaa",
        sha_after="bbb",
    )

    result = recall(store, paths=["shared.py"], caller_cwd=root)
    timeline = result["shared.py"]["timeline"]
    assert timeline == [], (
        "a prefix-named sibling repo's touch rows came back through the privacy wall"
    )


# ---- resume is a capability ------------------------------------------------


def test_resume_true_when_harness_supports_and_session_id_exists(store, tmp_path):
    """``resume: true`` when the harness can resume AND a session_id exists."""
    root = _setup_repo(tmp_path)
    p = _participant(store, cwd=root, harness="vibe", session_id="ses-1")
    _job(store, handle="h1", target_id=p.id)
    _touch(
        store,
        job_handle="h1",
        path="f.py",
        sha_before="aaa",
        sha_after="bbb",
    )

    result = recall(store, paths=["f.py"], caller_cwd=root)
    point = result["f.py"]["timeline"][0]
    assert point["resume"] is True
    assert "resume_note" not in point


def test_resume_false_when_no_session_id(store, tmp_path):
    """A harness that can resume but has no recorded session_id cannot
    actually resume — the caller learns this here, not at spawn time."""
    root = _setup_repo(tmp_path)
    p = _participant(store, cwd=root, harness="vibe", session_id=None)
    _job(store, handle="h1", target_id=p.id)
    _touch(
        store,
        job_handle="h1",
        path="f.py",
        sha_before="aaa",
        sha_after="bbb",
    )

    result = recall(store, paths=["f.py"], caller_cwd=root)
    point = result["f.py"]["timeline"][0]
    assert point["resume"] is False
    assert "no session id" in point["resume_note"]


def test_resume_false_when_harness_not_registered(store, tmp_path):
    """An unregistered harness cannot resume — the adapter does not exist."""
    root = _setup_repo(tmp_path)
    p = _participant(store, cwd=root, harness="nonexistent", session_id="ses-1")
    _job(store, handle="h1", target_id=p.id)
    _touch(
        store,
        job_handle="h1",
        path="f.py",
        sha_before="aaa",
        sha_after="bbb",
    )

    result = recall(store, paths=["f.py"], caller_cwd=root)
    point = result["f.py"]["timeline"][0]
    assert point["resume"] is False
    assert "not registered" in point["resume_note"]


# ---- reads are a count, not timeline points -------------------------------


def test_reads_are_counted_not_rendered(store, tmp_path):
    """A read (sha_before == sha_after) is a count, not a timeline point.

    Rendering reads as points buries the writes — nine reads to four
    writes in the sample. One integer per path keeps the signal
    without the noise.
    """
    root = _setup_repo(tmp_path)
    p = _participant(store, cwd=root)

    # Three reads (sha_before == sha_after).
    for i in range(3):
        _job(store, handle=f"r{i}", target_id=p.id, finished_at=1000.0 + i)
        _touch(
            store,
            job_handle=f"r{i}",
            path="f.py",
            mode="read",
            sha_before="same",
            sha_after="same",
        )

    # One write.
    _job(store, handle="w0", target_id=p.id, finished_at=2000.0)
    _touch(
        store,
        job_handle="w0",
        path="f.py",
        sha_before="same",
        sha_after="changed",
    )

    result = recall(store, paths=["f.py"], caller_cwd=root)
    assert result["f.py"]["reads"] == 3
    assert len(result["f.py"]["timeline"]) == 1
    assert result["f.py"]["timeline"][0]["handle"] == "w0"


# ---- task and result clip to ~300 characters --------------------------------


def test_task_and_result_are_clipped(store, tmp_path):
    """``task`` and ``result`` clip to ~300 characters. Full text lives
    behind the sibling's tool."""
    root = _setup_repo(tmp_path)
    p = _participant(store, cwd=root)
    long_task = "x" * 500
    long_result = "y" * 500
    _job(
        store,
        handle="h1",
        target_id=p.id,
        prompt=long_task,
        result=long_result,
    )
    _touch(
        store,
        job_handle="h1",
        path="f.py",
        sha_before="aaa",
        sha_after="bbb",
    )

    result = recall(store, paths=["f.py"], caller_cwd=root)
    point = result["f.py"]["timeline"][0]
    assert len(point["task"]) == CLIP
    assert len(point["result"]) == CLIP


# ---- depth caps the timeline -----------------------------------------------


def test_depth_caps_timeline_after_gaps(store, tmp_path):
    """Depth caps points per path, counted after gaps are interleaved.

    A depth of 2 means two points total — not two jobs plus the gaps
    between them.
    """
    root = _setup_repo(tmp_path)
    p = _participant(store, cwd=root)

    # Three jobs, all with intact chains (no gaps).
    for i in range(3):
        _job(store, handle=f"j{i}", target_id=p.id, finished_at=1000.0 + i)
        _touch(
            store,
            job_handle=f"j{i}",
            path="f.py",
            sha_before=f"s{i}",
            sha_after=f"s{i + 1}",
        )

    result = recall(store, paths=["f.py"], depth=2, caller_cwd=root)
    assert len(result["f.py"]["timeline"]) == 2
    # Newest first.
    assert result["f.py"]["timeline"][0]["handle"] == "j2"


# ---- a path that has never been touched ------------------------------------


def test_never_touched_path_returns_empty_timeline(store, tmp_path):
    """A path with no touch rows comes back as an empty timeline — not an
    error, not a missing key. The caller asked about it; answer about it."""
    root = _setup_repo(tmp_path)

    result = recall(store, paths=["never_touched.py"], caller_cwd=root)
    assert "never_touched.py" in result
    assert result["never_touched.py"]["timeline"] == []
    assert result["never_touched.py"]["reads"] == 0


# ---- path normalisation ----------------------------------------------------


def test_absolute_paths_normalised_to_repo_relative(store, tmp_path):
    """Absolute paths are stripped of the git root prefix before querying."""
    root = _setup_repo(tmp_path)
    p = _participant(store, cwd=root)
    _job(store, handle="h1", target_id=p.id)
    _touch(
        store,
        job_handle="h1",
        path="src/main.py",
        sha_before="aaa",
        sha_after="bbb",
    )

    # Query with an absolute path.
    abs_path = f"{root}/src/main.py"
    result = recall(store, paths=[abs_path], caller_cwd=root)

    # The result is keyed by the repo-relative path.
    assert "src/main.py" in result
    assert len(result["src/main.py"]["timeline"]) == 1


# ---- current and dirty -----------------------------------------------------


def test_current_is_blob_sha_of_file_on_disk(store, tmp_path):
    """``current`` is the file's blob sha right now — computed with
    ``blob_sha``, not a fork."""
    from theater.daemon.blob import blob_sha

    root = _setup_repo(tmp_path)
    p = _participant(store, cwd=root)
    _job(store, handle="h1", target_id=p.id)
    _touch(
        store,
        job_handle="h1",
        path="f.py",
        sha_before="aaa",
        sha_after="bbb",
    )

    f = tmp_path / "f.py"
    f.write_bytes(b"hello world")

    result = recall(store, paths=["f.py"], caller_cwd=root)
    assert result["f.py"]["current"] == blob_sha(f)


def test_dirty_true_when_working_tree_differs_from_head(store, tmp_path):
    """``dirty`` means the working tree differs from HEAD.

    Comes from the one ``git status --porcelain`` call. An uncommitted
    edit makes the file dirty.
    """
    root = _setup_repo(tmp_path)
    p = _participant(store, cwd=root)
    _job(store, handle="h1", target_id=p.id)
    _touch(
        store,
        job_handle="h1",
        path="f.py",
        sha_before="aaa",
        sha_after="bbb",
    )

    # Create and commit the file, then modify it.
    f = tmp_path / "f.py"
    f.write_bytes(b"committed")
    subprocess.run(
        ["git", "add", "f.py"],
        cwd=root,
        capture_output=True,
        timeout=10,
        check=False,
    )
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=root,
        capture_output=True,
        timeout=10,
        check=False,
    )
    f.write_bytes(b"modified")

    result = recall(store, paths=["f.py"], caller_cwd=root)
    assert result["f.py"]["dirty"] is True


def test_dirty_false_when_file_matches_head(store, tmp_path):
    """A file that matches HEAD is not dirty."""
    root = _setup_repo(tmp_path)
    p = _participant(store, cwd=root)
    _job(store, handle="h1", target_id=p.id)
    _touch(
        store,
        job_handle="h1",
        path="f.py",
        sha_before="aaa",
        sha_after="bbb",
    )

    f = tmp_path / "f.py"
    f.write_bytes(b"committed")
    subprocess.run(
        ["git", "add", "f.py"],
        cwd=root,
        capture_output=True,
        timeout=10,
        check=False,
    )
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=root,
        capture_output=True,
        timeout=10,
        check=False,
    )

    result = recall(store, paths=["f.py"], caller_cwd=root)
    assert result["f.py"]["dirty"] is False


# ---- the segment id format -------------------------------------------------


def test_job_point_segment_id_is_job_handle(store, tmp_path):
    """For a job point, ``segment`` is the job handle verbatim."""
    root = _setup_repo(tmp_path)
    p = _participant(store, cwd=root)
    _job(store, handle="codex-a41f", target_id=p.id)
    _touch(
        store,
        job_handle="codex-a41f",
        path="f.py",
        sha_before="aaa",
        sha_after="bbb",
    )

    result = recall(store, paths=["f.py"], caller_cwd=root)
    assert result["f.py"]["timeline"][0]["segment"] == "codex-a41f"


# ---- empty paths -----------------------------------------------------------


def test_empty_paths_returns_empty_dict(store):
    """Asking about nothing gets nothing back — not an error."""
    assert recall(store, paths=[]) == {}


# ---- result is keyed by path, no cross-path ranking ------------------------


def test_result_keyed_by_path_independently(store, tmp_path):
    """Each path gets its own independent timeline. No cross-path ranking
    — that was explicitly excluded from the design."""
    root = _setup_repo(tmp_path)
    p = _participant(store, cwd=root)

    for path in ("a.py", "b.py"):
        _job(store, handle=f"h-{path}", target_id=p.id)
        _touch(
            store,
            job_handle=f"h-{path}",
            path=path,
            sha_before="aaa",
            sha_after="bbb",
        )

    result = recall(store, paths=["a.py", "b.py"], caller_cwd=root)
    assert set(result.keys()) == {"a.py", "b.py"}
    assert len(result["a.py"]["timeline"]) == 1
    assert len(result["b.py"]["timeline"]) == 1


# ---- the sha field format --------------------------------------------------


def test_sha_field_shows_before_arrow_after(store, tmp_path):
    """The ``sha`` field renders as ``before → after``, with ``-`` for null."""
    root = _setup_repo(tmp_path)
    p = _participant(store, cwd=root)
    _job(store, handle="h1", target_id=p.id)
    _touch(
        store,
        job_handle="h1",
        path="f.py",
        sha_before=None,  # file was created
        sha_after="bbb",
    )

    result = recall(store, paths=["f.py"], caller_cwd=root)
    point = result["f.py"]["timeline"][0]
    assert point["sha"] == "- → bbb"


# ---- git_root helper -------------------------------------------------------


def test_git_root_returns_none_outside_repo(tmp_path):
    """A directory that is not a git repo returns None from _git_root."""
    # tmp_path might be inside a git repo on some systems; use a subdir
    # that is definitely not tracked.
    import tempfile

    d = tempfile.mkdtemp(dir="/tmp")
    assert _git_root(d) is None


# ---- lineage on a timeline point -------------------------------------------


def test_point_reports_the_parent_of_the_editing_session(store, tmp_path):
    """A child's edit names the agent that spawned it.

    The question recall answers is "who touched this file"; the follow-up
    is "on whose orders". ``parent_id`` is the lineage half of that and
    comes free from the participants join already in the query.
    """
    root = _setup_repo(tmp_path)
    _participant(store, pid="boss", cwd=root)
    child = _participant(store, pid="kid", cwd=root, parent_id="boss")
    _job(store, handle="h1", target_id=child.id)
    _touch(store, job_handle="h1", path="f.py", sha_before="aaa", sha_after="bbb")

    point = recall(store, paths=["f.py"], caller_cwd=root)["f.py"]["timeline"][0]
    assert point["parent_id"] == "boss"


def test_root_authored_point_reports_no_parent(store, tmp_path):
    """A participant nobody spawned reports ``parent_id`` as None.

    Explicitly None rather than absent: the caller can tell "this was a
    root" from "this recall build predates lineage".
    """
    root = _setup_repo(tmp_path)
    p = _participant(store, pid="solo", cwd=root)
    _job(store, handle="h1", target_id=p.id)
    _touch(store, job_handle="h1", path="f.py", sha_before="aaa", sha_after="bbb")

    point = recall(store, paths=["f.py"], caller_cwd=root)["f.py"]["timeline"][0]
    assert point["parent_id"] is None


def test_caller_id_can_differ_from_parent_id(store, tmp_path):
    """Who ordered the job is not always who spawned the agent.

    A ``send`` from a sibling produces a job whose caller is not the
    target's parent. Collapsing the two into one field would report the
    wrong agent for every mid-session prompt.
    """
    root = _setup_repo(tmp_path)
    child = _participant(store, pid="kid", cwd=root, parent_id="boss")
    _job(store, handle="h1", target_id=child.id, caller_id="sibling")
    _touch(store, job_handle="h1", path="f.py", sha_before="aaa", sha_after="bbb")

    point = recall(store, paths=["f.py"], caller_cwd=root)["f.py"]["timeline"][0]
    assert point["caller_id"] == "sibling"
    assert point["parent_id"] == "boss"


def test_gap_point_carries_no_lineage(store, tmp_path):
    """A gap belongs to no job, so it has no parent and no caller.

    Gap points are built from a broken hash chain, not from a
    participant row. Inventing lineage for them would attribute an
    unclaimed edit to whichever job happens to sit next to it.
    """
    root = _setup_repo(tmp_path)
    p = _participant(store, pid="kid", cwd=root, parent_id="boss")
    _job(store, handle="old", target_id=p.id, finished_at=1000.0)
    _touch(store, job_handle="old", path="f.py", sha_before="aaa", sha_after="bbb")
    _job(store, handle="new", target_id=p.id, finished_at=2000.0)
    _touch(store, job_handle="new", path="f.py", sha_before="ccc", sha_after="ddd")

    timeline = recall(store, paths=["f.py"], caller_cwd=root)["f.py"]["timeline"]
    gap = timeline[1]
    assert gap["gap"] is True
    assert "parent_id" not in gap
    assert "caller_id" not in gap


# ---- the RPC layer decorates lineage with runtime names --------------------


def test_attach_parent_names_resolves_known_parents(registry):
    """``parent_name`` is attached above recall, where the Registry lives.

    ``recall.py`` takes only a Store; names are Registry state. A parent
    the Registry cannot resolve degrades to None rather than raising,
    because the id alone still answers the question.
    """
    import types

    from theater.daemon.methods import _attach_parent_names

    boss = registry.register(harness="vibe", pane=None, cwd="/tmp/repo")
    result = {
        "f.py": {
            "timeline": [
                {"parent_id": boss.id},
                {"parent_id": "ghost"},
                {"parent_id": None},
                {"gap": True},
            ]
        }
    }
    _attach_parent_names(types.SimpleNamespace(registry=registry), result)

    points = result["f.py"]["timeline"]
    expected = registry.get(boss.id).name
    assert expected  # the Registry names lazily; a None here proves nothing
    assert points[0]["parent_name"] == expected
    assert points[1]["parent_name"] is None
    assert "parent_name" not in points[2]
    assert "parent_name" not in points[3]
