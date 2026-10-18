"""Recall timelines: gaps, the privacy wall, resumability, clipping and depth."""

from __future__ import annotations

import json

import pytest

from tests.support.recall_rows import add_job, add_participant, add_touch_row, setup_repo
from theater.daemon.recall import (
    CLIP,
    recall,
    task_without_guidance,
)
from theater.daemon.recall_read import read_segment
from theater.daemon.response_contracts.contract import StrictContract
from theater.daemon.response_contracts.prompts import prompt_with_guidance, response_guidance
from theater.models import JobState, Participant, Tier


def test_crashed_job_appears_in_timeline(store, tmp_path):
    """A crashed job wrote to the file and its sha is in the chain.

    Outcome sorts a job down, it never filters it out. Somebody has to
    know an incomplete edit landed — that is the whole point of not
    silently dropping it.
    """
    root = setup_repo(tmp_path)
    p = add_participant(store, cwd=root, session_id="ses-crash")
    add_job(
        store,
        handle="crashed-job",
        target_id=p.id,
        state=JobState.CRASHED,
        result=None,
    )
    add_touch_row(
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


def test_unavailable_hash_touch_stays_in_timeline(store, tmp_path):
    root = setup_repo(tmp_path)
    p = add_participant(store, cwd=root)
    add_job(store, handle="large-file-job", target_id=p.id)
    add_touch_row(
        store,
        job_handle="large-file-job",
        path="large.lock",
        sha_before_error="too_large",
        sha_after_error="too_large",
    )

    result = recall(store, paths=["large.lock"], caller_cwd=root)

    assert result["large.lock"]["reads"] == 0
    point = result["large.lock"]["timeline"][0]
    assert point["handle"] == "large-file-job"
    assert point["sha"] == "? → ?"
    assert point["sha_before_error"] == "too_large"
    assert point["sha_after_error"] == "too_large"


def test_gap_emitted_when_chain_breaks(store, tmp_path):
    """A mismatch between sha_after and the next sha_before is a gap.

    Job A left the file at ``bbb``; job B found it at ``ccc``. Nothing
    in touch explains the transition. A gap point must appear between
    them, with the right segment id format.
    """
    root = setup_repo(tmp_path)
    p = add_participant(store, cwd=root)

    # Older job (finished first).
    add_job(store, handle="older", target_id=p.id, finished_at=1000.0)
    add_touch_row(
        store,
        job_handle="older",
        path="f.py",
        sha_before="aaa",
        sha_after="bbb",
    )

    # Newer job (finished second, so it sorts above).
    add_job(store, handle="newer", target_id=p.id, finished_at=2000.0)
    add_touch_row(
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
    root = setup_repo(tmp_path)
    p = add_participant(store, cwd=root)

    add_job(store, handle="first", target_id=p.id, finished_at=1000.0)
    add_touch_row(
        store,
        job_handle="first",
        path="f.py",
        sha_before="aaa",
        sha_after="bbb",
    )

    add_job(store, handle="second", target_id=p.id, finished_at=2000.0)
    add_touch_row(
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
    root = setup_repo(tmp_path)
    p = add_participant(store, cwd=root)

    # Job A leaves sha_after="aaa" (file exists at sha aaa).
    add_job(store, handle="first", target_id=p.id, finished_at=1000.0)
    add_touch_row(
        store,
        job_handle="first",
        path="gone.py",
        sha_before="bbb",
        sha_after="aaa",
    )

    # Job B finds sha_before=None (file did not exist). Gap: something
    # deleted the file between A's sha_after and B's sha_before.
    add_job(store, handle="second", target_id=p.id, finished_at=2000.0)
    add_touch_row(
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
    root_a = setup_repo(repo_a)
    root_b = setup_repo(repo_b)

    # Participant in repo B touches the same path.
    p_b = add_participant(store, pid="p-b", cwd=root_b)
    add_job(store, handle="h-b", target_id=p_b.id)
    add_touch_row(
        store,
        job_handle="h-b",
        path="shared.py",
        sha_before="aaa",
        sha_after="bbb",
    )

    # Participant in repo A touches the same path.
    p_a = add_participant(store, pid="p-a", cwd=root_a)
    add_job(store, handle="h-a", target_id=p_a.id)
    add_touch_row(
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
    root = setup_repo(tmp_path)

    # Participant with null cwd.
    p_null = Participant(
        id="p-null",
        harness="vibe",
        tier=Tier.SPAWNED,
        cwd=None,
    )
    store.upsert_participant(p_null)
    add_job(store, handle="h-null", target_id="p-null")
    add_touch_row(
        store,
        job_handle="h-null",
        path="x.py",
        sha_before="aaa",
        sha_after="bbb",
    )

    # Participant in the real repo.
    p = add_participant(store, pid="p-real", cwd=root)
    add_job(store, handle="h-real", target_id=p.id)
    add_touch_row(
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
    root = setup_repo(mine)

    # Never created on disk — it only has to exist as a cwd string in
    # the participants table for the wall to have something to leak.
    sibling = str((tmp_path / "myXrepo").resolve())
    p = add_participant(store, pid="p-sibling", cwd=sibling)
    add_job(store, handle="h-sibling", target_id=p.id)
    add_touch_row(
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
    root = setup_repo(tmp_path)
    child_cwd = f"{root}/.theater/worktrees/child-abc"
    p = add_participant(store, pid="p-child", cwd=child_cwd, session_id="ses-child")
    add_job(
        store,
        handle="h-child",
        target_id=p.id,
        prompt="fix the bug",
        result="all good",
    )
    add_touch_row(
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
    root = setup_repo(repo)

    sibling_cwd = str((tmp_path / "repo-secret").resolve())
    p = add_participant(
        store,
        pid="p-sibling",
        cwd=sibling_cwd,
        session_id="ses-secret",
    )
    add_job(
        store,
        handle="h-secret",
        target_id=p.id,
        prompt="top secret prompt",
        result="top secret result",
    )
    add_touch_row(
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


@pytest.mark.parametrize(
    ("harness", "session_id", "correlation", "resumable", "note"),
    [
        pytest.param("vibe", "ses-1", "exact", True, None, id="harness-and-exact-session"),
        pytest.param("vibe", "ses-1", "heuristic", False, "cwd/time", id="heuristic-session"),
        # The caller learns a missing session id here, not at spawn time.
        pytest.param("vibe", None, None, False, "no session id", id="no-session-id"),
        # An unregistered harness has no adapter to resume with.
        pytest.param(
            "nonexistent", "ses-1", None, False, "not registered", id="unregistered-harness"
        ),
    ],
)
def test_resume_is_reported_as_a_capability(
    store, tmp_path, harness, session_id, correlation, resumable, note
):
    """``resume`` is true only when the harness can resume AND a trusted session_id exists."""
    root = setup_repo(tmp_path)
    p = add_participant(store, cwd=root, harness=harness, session_id=session_id)
    if correlation is not None:
        p.session_correlation = correlation
        store.upsert_participant(p)
    add_job(store, handle="h1", target_id=p.id)
    add_touch_row(
        store,
        job_handle="h1",
        path="f.py",
        sha_before="aaa",
        sha_after="bbb",
    )

    result = recall(store, paths=["f.py"], caller_cwd=root)
    point = result["f.py"]["timeline"][0]
    assert point["resume"] is resumable
    if note is None:
        assert "resume_note" not in point
    else:
        assert note in point["resume_note"]


def test_reads_are_counted_not_rendered(store, tmp_path):
    """A read (sha_before == sha_after) is a count, not a timeline point.

    Rendering reads as points buries the writes — nine reads to four
    writes in the sample. One integer per path keeps the signal
    without the noise.
    """
    root = setup_repo(tmp_path)
    p = add_participant(store, cwd=root)

    # Three reads (sha_before == sha_after).
    for i in range(3):
        add_job(store, handle=f"r{i}", target_id=p.id, finished_at=1000.0 + i)
        add_touch_row(
            store,
            job_handle=f"r{i}",
            path="f.py",
            mode="read",
            sha_before="same",
            sha_after="same",
        )

    # One write.
    add_job(store, handle="w0", target_id=p.id, finished_at=2000.0)
    add_touch_row(
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


@pytest.mark.parametrize(
    "response_format",
    [
        None,
        json.dumps({"type": "string", "description": "x" * 400}),
        StrictContract("custom", '{"type":"string"}', "answer").envelope(),
    ],
)
async def test_task_and_result_are_clipped(store, registry, tmp_path, response_format):
    """Both recall surfaces recover the task before the timeline clips it."""
    root = setup_repo(tmp_path)
    p = add_participant(store, cwd=root)
    long_task = "x" * 500
    long_result = "y" * 500
    add_job(
        store,
        handle="h1",
        target_id=p.id,
        prompt=long_task
        if response_format is None
        else prompt_with_guidance(long_task, response_format),
        response_format=response_format,
        result=long_result,
    )
    add_touch_row(
        store,
        job_handle="h1",
        path="f.py",
        sha_before="aaa",
        sha_after="bbb",
    )

    result = recall(store, paths=["f.py"], caller_cwd=root)
    point = result["f.py"]["timeline"][0]
    assert point["task"] == long_task[:CLIP] + f"… (+{len(long_task) - CLIP} chars)"
    assert point["result"] == long_result[:CLIP] + f"… (+{len(long_result) - CLIP} chars)"
    brief = await read_segment("h1", store=store, registry=registry, cwd=root)
    assert brief["task"] == long_task


@pytest.mark.parametrize("strict", [False, True])
def test_guidance_removal_preserves_user_text(strict):
    schema = '{"type":"string"}'
    response_format = StrictContract("custom", schema, "answer").envelope() if strict else schema
    guidance = response_guidance(response_format)
    task = f"Keep these words verbatim:\n\n{guidance}\n\nmore user text"
    assert (
        task_without_guidance(prompt_with_guidance(task, response_format), response_format) == task
    )
    assert task_without_guidance(task, response_format) == task
    assert task_without_guidance(guidance, None) == guidance
    changed = prompt_with_guidance(task, response_format).replace("single bare", "one bare")
    assert task_without_guidance(changed, response_format) == changed
    assert task_without_guidance(prompt_with_guidance("", response_format), response_format) == ""


def test_depth_caps_timeline_after_gaps(store, tmp_path):
    """Depth caps points per path, counted after gaps are interleaved.

    A depth of 2 means two points total — not two jobs plus the gaps
    between them.
    """
    root = setup_repo(tmp_path)
    p = add_participant(store, cwd=root)

    # Three jobs, all with intact chains (no gaps).
    for i in range(3):
        add_job(store, handle=f"j{i}", target_id=p.id, finished_at=1000.0 + i)
        add_touch_row(
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


def test_job_point_segment_id_is_job_handle(store, tmp_path):
    """For a job point, ``segment`` is the job handle verbatim."""
    root = setup_repo(tmp_path)
    p = add_participant(store, cwd=root)
    add_job(store, handle="codex-a41f", target_id=p.id)
    add_touch_row(
        store,
        job_handle="codex-a41f",
        path="f.py",
        sha_before="aaa",
        sha_after="bbb",
    )

    result = recall(store, paths=["f.py"], caller_cwd=root)
    assert result["f.py"]["timeline"][0]["segment"] == "codex-a41f"


def test_empty_paths_returns_empty_dict(store):
    """Asking about nothing gets nothing back — not an error."""
    assert recall(store, paths=[]) == {}


def test_result_keyed_by_path_independently(store, tmp_path):
    """Each path gets its own independent timeline. No cross-path ranking
    — that was explicitly excluded from the design."""
    root = setup_repo(tmp_path)
    p = add_participant(store, cwd=root)

    for path in ("a.py", "b.py"):
        add_job(store, handle=f"h-{path}", target_id=p.id)
        add_touch_row(
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


def test_sha_field_shows_before_arrow_after(store, tmp_path):
    """The ``sha`` field renders as ``before → after``, with ``-`` for null."""
    root = setup_repo(tmp_path)
    p = add_participant(store, cwd=root)
    add_job(store, handle="h1", target_id=p.id)
    add_touch_row(
        store,
        job_handle="h1",
        path="f.py",
        sha_before=None,  # file was created
        sha_after="bbb",
    )

    result = recall(store, paths=["f.py"], caller_cwd=root)
    point = result["f.py"]["timeline"][0]
    assert point["sha"] == "- → bbb"
