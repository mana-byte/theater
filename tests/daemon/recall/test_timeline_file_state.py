"""Recall's git-backed file state: fork budget, path normalisation, current and dirty."""

from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

import theater.daemon.recall as recall_mod
from tests.support.recall_rows import add_job, add_participant, add_touch_row, setup_repo
from theater.daemon.blob import BlobHash, BlobHashState
from theater.daemon.recall import (
    _git_root,
    hash_current_files,
    recall,
)


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
    root = setup_repo(tmp_path)

    # Seed touch rows for 20 paths so the query has something to chew on.
    p = add_participant(store, cwd=root)
    add_job(store, handle="h1", target_id=p.id)
    for i in range(20):
        add_touch_row(
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


def test_never_touched_path_returns_empty_timeline(store, tmp_path):
    """A path with no touch rows comes back as an empty timeline — not an
    error, not a missing key. The caller asked about it; answer about it."""
    root = setup_repo(tmp_path)

    result = recall(store, paths=["never_touched.py"], caller_cwd=root)
    assert "never_touched.py" in result
    assert result["never_touched.py"]["timeline"] == []
    assert result["never_touched.py"]["reads"] == 0


def test_absolute_paths_normalised_to_repo_relative(store, tmp_path):
    """Absolute paths are stripped of the git root prefix before querying."""
    root = setup_repo(tmp_path)
    p = add_participant(store, cwd=root)
    add_job(store, handle="h1", target_id=p.id)
    add_touch_row(
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


@pytest.mark.parametrize("requested", ["absolute", "traversal", "symlink"])
def test_recall_rejects_paths_outside_repo(store, tmp_path, requested):
    repo = tmp_path / "repo"
    repo.mkdir()
    root = setup_repo(repo)
    outside = tmp_path / "secret.txt"
    outside.write_text("secret")
    if requested == "absolute":
        path = str(outside)
    elif requested == "traversal":
        path = "../secret.txt"
    else:
        (repo / "escape").symlink_to(outside)
        path = "escape"

    with (
        patch("theater.daemon.recall.blob_hash") as hasher,
        pytest.raises(ValueError, match="outside the repository"),
    ):
        recall(store, paths=[path], caller_cwd=root)

    hasher.assert_not_called()


def test_recall_hashes_have_one_query_budget(tmp_path, monkeypatch):
    for name, body in (("a", b"a"), ("b", b"bb"), ("c", b"c")):
        (tmp_path / name).write_bytes(body)
    monkeypatch.setattr(recall_mod, "RECALL_HASH_MAX_QUERY_BYTES", 3)

    result = hash_current_files(str(tmp_path), ["a", "b", "c"])

    assert result["a"].state is BlobHashState.HASHED
    assert result["b"].state is BlobHashState.HASHED
    assert result["c"].state is BlobHashState.UNAVAILABLE
    assert sum(item.size for item in result.values() if item.state is BlobHashState.HASHED) == 3


def test_recall_hash_budget_charges_failed_reads(tmp_path, monkeypatch):
    monkeypatch.setattr(recall_mod, "RECALL_HASH_MAX_QUERY_BYTES", 3)
    allowances = []

    def failing_hash(_path, *, max_bytes):
        allowances.append(max_bytes)
        return BlobHash(BlobHashState.UNAVAILABLE, reason="changed_while_reading")

    monkeypatch.setattr(recall_mod, "blob_hash", failing_hash)

    hash_current_files(str(tmp_path), ["a", "b"])

    assert allowances == [3, 0]


def test_current_is_blob_sha_of_file_on_disk(store, tmp_path):
    """``current`` is the file's blob sha right now — computed with
    ``blob_sha``, not a fork."""
    from theater.daemon.blob import blob_sha

    root = setup_repo(tmp_path)
    p = add_participant(store, cwd=root)
    add_job(store, handle="h1", target_id=p.id)
    add_touch_row(
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
    assert result["f.py"]["current_status"] == "hashed"


def test_current_distinguishes_unavailable_hash_from_missing_file(store, tmp_path):
    from theater.constants.daemon import TOUCH_HASH_MAX_FILE_BYTES

    root = setup_repo(tmp_path)
    path = tmp_path / "large.bin"
    path.touch()
    with path.open("r+b") as stream:
        stream.truncate(TOUCH_HASH_MAX_FILE_BYTES + 1)

    result = recall(store, paths=["large.bin"], caller_cwd=root)

    assert result["large.bin"]["current"] is None
    assert result["large.bin"]["current_status"] == "unavailable"
    assert result["large.bin"]["current_error"] == "too_large"


def test_dirty_true_when_working_tree_differs_from_head(store, tmp_path):
    """``dirty`` means the working tree differs from HEAD.

    Comes from the one ``git status --porcelain`` call. An uncommitted
    edit makes the file dirty.
    """
    root = setup_repo(tmp_path)
    p = add_participant(store, cwd=root)
    add_job(store, handle="h1", target_id=p.id)
    add_touch_row(
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
    root = setup_repo(tmp_path)
    p = add_participant(store, cwd=root)
    add_job(store, handle="h1", target_id=p.id)
    add_touch_row(
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


def test_git_root_returns_none_outside_repo(tmp_path):
    """A directory that is not a git repo returns None from _git_root."""
    # tmp_path might be inside a git repo on some systems; use a subdir
    # that is definitely not tracked.
    import tempfile

    d = tempfile.mkdtemp(dir="/tmp")
    assert _git_root(d) is None


def test_precomputed_root_none_does_not_recall_git_root(monkeypatch, tmp_path, store):
    """Passing precomputed_root=None explicitly (the "cwd is not a git repo"
    case) must NOT make recall() re-call _git_root synchronously — that
    would reintroduce the blocking pattern this migration eliminated.
    The _UNSET sentinel distinguishes "not provided" from "provided but None".
    """

    # If the sentinel is broken (None instead of _UNSET), recall() would
    # call _git_root again. Patch it to blow up if called.
    def boom(cwd):
        raise AssertionError("_git_root should not be called when precomputed_root is provided")

    monkeypatch.setattr("theater.daemon.recall._git_root", boom)

    # precomputed_root=None means "already checked, not a git repo."
    # recall() should use it directly, not call _git_root again.
    result = recall(
        store,
        paths=["x"],
        caller_cwd=str(tmp_path),
        precomputed_root=None,
        precomputed_dirty=set(),
    )
    # paths=["x"] with an empty store → empty timeline, no error
    assert result["x"]["timeline"] == []
    assert result["x"]["current"] is None
    assert result["x"]["current_status"] == "missing"
