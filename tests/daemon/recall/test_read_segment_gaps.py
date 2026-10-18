"""Reading a gap segment from git, and read-only behaviour of segment reads."""

from __future__ import annotations

from sqlalchemy import select

from tests.support.recall_segments import blob_sha_at, commit_all, init_git, make_job
from tests.support.shipped import VibeHarness
from theater.daemon.recall_read import read_segment


async def test_gap_segment_git_can_explain(tmp_path):
    """A gap that git can explain returns the commits that touched the
    path between the two blob shas."""
    repo = tmp_path / "repo"
    init_git(repo)

    # Create a file, commit it.
    f = repo / "src.py"
    f.write_text("original\n")
    commit_all(repo, "initial")
    before_blob = blob_sha_at(repo, "HEAD:src.py")

    # Modify the file, commit again.
    f.write_text("modified\n")
    commit_all(repo, "change it")
    after_blob = blob_sha_at(repo, "HEAD:src.py")

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
    init_git(repo)

    # Create and commit a file.
    f = repo / "src.py"
    f.write_text("original\n")
    commit_all(repo, "initial")

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
    init_git(repo)

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
    init_git(repo)
    f = repo / "src.py"
    f.write_text("hello\n")
    commit_all(repo, "create")
    blob = blob_sha_at(repo, "HEAD:src.py")

    # ``-`` means the file did not exist before (creation).
    segment = f"gap:src.py:-..{blob}"
    result = await read_segment(segment, store=None, registry=None, cwd=str(repo))

    assert result["kind"] == "gap"
    assert result["sha_before"] is None
    assert result["sha_after"] == blob


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

    make_job(
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
    init_git(repo)
    await read_segment(
        "gap:src.py:aaa..bbb",
        store=registry.store,
        registry=registry,
        cwd=str(repo),
    )

    after = registry.store.conn.execute(select(func.count()).select_from(bus)).scalar()
    assert before == after
