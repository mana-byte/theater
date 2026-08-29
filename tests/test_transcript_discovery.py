"""Focused tests for the generic GlobDiscovery strategy."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from theater.harness.transcript.discovery import (
    GlobDiscovery,
    parent_birthtime,
    screen_tail,
    stat_birthtime,
)


def _make_jsonl(path: Path, cwd: str | None = None, session_id: str | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    record: dict[str, object] = {"type": "assistant", "message": {"content": []}}
    if cwd:
        record["cwd"] = cwd
    if session_id:
        record["sessionId"] = session_id
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    return path


def _claude_discovery(root: Path) -> GlobDiscovery:
    """A minimal Claude-shaped discovery for testing the generic strategy."""

    def session_id_of(path: Path) -> str | None:
        return path.stem if path.suffix == ".jsonl" and path.stem else None

    def cwd_of(path: Path) -> str | None:
        try:
            with path.open(encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    record = json.loads(line)
                    found = record.get("cwd") if isinstance(record, dict) else None
                    if found:
                        return str(Path(found).resolve())
        except (OSError, ValueError):
            return None
        return None

    def is_shape(path: Path, *, root: Path) -> bool:
        if path.suffix != ".jsonl":
            return False
        return path.parent.parent.resolve() == root.resolve()

    return GlobDiscovery(
        root=root,
        glob_pattern="*/*.jsonl",
        session_id_of=session_id_of,
        cwd_of=cwd_of,
        is_shape=is_shape,
        birthtime_of=stat_birthtime,
        loss_probes=8,
        collision_warning="test: %d transcripts match cwd %s",
    )


@pytest.fixture
def root(tmp_path) -> Path:
    return tmp_path / "projects"


@pytest.fixture
def workdir(tmp_path) -> str:
    d = tmp_path / "work"
    d.mkdir()
    return str(d)


class TestAdmitOperatorCandidate:
    def test_symlink_rejected(self, root, workdir):
        d = root / "project"
        d.mkdir(parents=True)
        target = _make_jsonl(d / "real.jsonl", cwd=workdir)
        link = d / "link.jsonl"
        link.symlink_to(target)
        disc = _claude_discovery(root)
        with pytest.raises(ValueError, match="symlink"):
            disc.admit_operator_candidate(cwd=workdir, candidate=str(link))

    def test_outside_root_rejected(self, root, workdir, tmp_path):
        outside = tmp_path / "outside" / "x.jsonl"
        _make_jsonl(outside, cwd=workdir)
        disc = _claude_discovery(root)
        with pytest.raises(ValueError, match="outside"):
            disc.admit_operator_candidate(cwd=workdir, candidate=str(outside))

    def test_not_readable(self, root, workdir):
        path = root / "proj" / "bad.jsonl"
        path.parent.mkdir(parents=True)
        disc = _claude_discovery(root)
        with pytest.raises(ValueError, match="not readable"):
            disc.admit_operator_candidate(cwd=workdir, candidate=str(path))

    def test_created_before_floor(self, root, workdir):
        path = _make_jsonl(root / "proj" / "old.jsonl", cwd=workdir)
        disc = _claude_discovery(root)
        far_future = time.time() + 10**9
        with pytest.raises(ValueError, match="created before participant floor"):
            disc.admit_operator_candidate(cwd=workdir, candidate=str(path), after=far_future)

    def test_shape_mismatch(self, root, workdir):
        path = root / "proj" / "deep" / "nested" / "bad.jsonl"
        _make_jsonl(path, cwd=workdir)
        disc = _claude_discovery(root)
        with pytest.raises(ValueError, match="harness shape mismatch"):
            disc.admit_operator_candidate(cwd=workdir, candidate=str(path))

    def test_unextractable_session_id(self, root, workdir):
        path = root / "proj" / "badname.jsonl"
        _make_jsonl(path, cwd=workdir)
        disc = GlobDiscovery(
            root=root,
            glob_pattern="*/*.jsonl",
            session_id_of=lambda p: None,
            cwd_of=lambda p: None,
            is_shape=lambda p, *, root: p.suffix == ".jsonl",
            birthtime_of=stat_birthtime,
            loss_probes=8,
            collision_warning="test: %d %s",
        )
        with pytest.raises(ValueError, match="unextractable session id"):
            disc.admit_operator_candidate(cwd=workdir, candidate=str(path))

    def test_cwd_mismatch(self, root, workdir, tmp_path):
        other = tmp_path / "other"
        other.mkdir()
        path = _make_jsonl(root / "proj" / "x.jsonl", cwd=str(other))
        disc = _claude_discovery(root)
        with pytest.raises(ValueError, match="cwd mismatch"):
            disc.admit_operator_candidate(cwd=workdir, candidate=str(path))

    def test_harness_mismatch_or_unextractable_cwd(self, root, workdir):
        path = root / "proj" / "nocwd.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"type": "assistant"}) + "\n", encoding="utf-8")
        disc = _claude_discovery(root)
        with pytest.raises(ValueError, match="harness mismatch or unextractable cwd"):
            disc.admit_operator_candidate(cwd=workdir, candidate=str(path))

    def test_valid_candidate(self, root, workdir):
        path = _make_jsonl(root / "proj" / "good.jsonl", cwd=workdir)
        disc = _claude_discovery(root)
        row = disc.admit_operator_candidate(cwd=workdir, candidate=str(path))
        assert row.rejection_reason is None
        assert row.session_id == "good"


class TestTranscriptCandidates:
    def test_newest_first_sort(self, root, workdir):
        _make_jsonl(root / "a" / "old.jsonl", cwd=workdir)
        time.sleep(0.01)
        path2 = _make_jsonl(root / "b" / "new.jsonl", cwd=workdir)
        disc = _claude_discovery(root)
        rows = disc.transcript_candidates(cwd=workdir)
        assert rows[0].location == str(path2)

    def test_empty_root(self, tmp_path):
        disc = _claude_discovery(tmp_path / "empty")
        assert disc.transcript_candidates(cwd="/work") == []

    def test_domain_override(self, root, workdir, tmp_path):
        alt = tmp_path / "alt"
        path = _make_jsonl(alt / "proj" / "x.jsonl", cwd=workdir)
        disc = _claude_discovery(root)
        rows = disc.transcript_candidates(cwd=workdir, domain=str(alt))
        assert any(r.location == str(path) for r in rows)


class TestIdentityLossCandidate:
    def test_bounded_probe_count(self, root, workdir):
        current = _make_jsonl(root / "a" / "current.jsonl", cwd=workdir)
        for i in range(20):
            _make_jsonl(root / f"d{i}" / f"new{i}.jsonl", cwd=workdir)
        disc = GlobDiscovery(
            root=root,
            glob_pattern="*/*.jsonl",
            session_id_of=lambda p: p.stem,
            cwd_of=lambda p: None,
            is_shape=lambda p, *, root: True,
            birthtime_of=stat_birthtime,
            loss_probes=3,
            collision_warning="test: %d %s",
        )
        result = disc.identity_loss_candidate(cwd=workdir, current=current, current_mtime_ns=0)
        assert result is None

    def test_rejected_candidates_do_not_consume_probe_budget(self, root, workdir):
        current = _make_jsonl(root / "a" / "current.jsonl", cwd=workdir)
        wanted = _make_jsonl(root / "b" / "wanted.jsonl", cwd=workdir)
        os.utime(current, ns=(1, 1))
        os.utime(wanted, ns=(2, 2))
        for index in range(4):
            rejected = _make_jsonl(root / f"r{index}" / f"rejected{index}.jsonl", cwd=workdir)
            os.utime(rejected, ns=(10 + index, 10 + index))
        disc = GlobDiscovery(
            root=root,
            glob_pattern="*/*.jsonl",
            session_id_of=lambda p: p.stem,
            cwd_of=lambda p: str(Path(workdir).resolve()),
            is_shape=lambda p, *, root: True,
            birthtime_of=stat_birthtime,
            loss_probes=1,
            collision_warning="test: %d %s",
            automatic_rejection_of=(
                lambda p: "not autonomous" if p.stem.startswith("rejected") else None
            ),
        )

        result = disc.identity_loss_candidate(
            cwd=workdir, current=current, current_mtime_ns=current.stat().st_mtime_ns
        )

        assert result == wanted

    def test_older_mtime_skipped(self, root, workdir):
        current = _make_jsonl(root / "a" / "current.jsonl", cwd=workdir)
        _make_jsonl(root / "b" / "older.jsonl", cwd=workdir)
        disc = _claude_discovery(root)
        result = disc.identity_loss_candidate(
            cwd=workdir,
            current=current,
            current_mtime_ns=current.stat().st_mtime_ns + 10**9,
        )
        assert result is None

    def test_symlink_skipped(self, root, workdir):
        current = _make_jsonl(root / "a" / "current.jsonl", cwd=workdir)
        target = _make_jsonl(root / "b" / "real.jsonl", cwd=workdir)
        link = root / "c" / "link.jsonl"
        link.parent.mkdir(parents=True)
        link.symlink_to(target)
        disc = _claude_discovery(root)
        result = disc.identity_loss_candidate(cwd=workdir, current=current, current_mtime_ns=0)
        assert result != link

    def test_current_excluded(self, root, workdir):
        current = _make_jsonl(root / "a" / "current.jsonl", cwd=workdir)
        disc = _claude_discovery(root)
        result = disc.identity_loss_candidate(cwd=workdir, current=current, current_mtime_ns=0)
        assert result != current

    def test_no_cwd_returns_none(self, root):
        disc = _claude_discovery(root)
        assert (
            disc.identity_loss_candidate(cwd=None, current=root / "x", current_mtime_ns=0) is None
        )


class TestFindTranscript:
    def test_no_root(self, tmp_path):
        disc = _claude_discovery(tmp_path / "missing")
        assert disc.find_transcript(cwd="/work") is None

    def test_no_match(self, root, workdir):
        _make_jsonl(root / "a" / "x.jsonl", cwd="/somewhere/else")
        disc = _claude_discovery(root)
        assert disc.find_transcript(cwd=workdir) is None

    def test_newest_match(self, root, workdir):
        _make_jsonl(root / "a" / "old.jsonl", cwd=workdir)
        time.sleep(0.01)
        path2 = _make_jsonl(root / "b" / "new.jsonl", cwd=workdir)
        disc = _claude_discovery(root)
        assert disc.find_transcript(cwd=workdir) == path2

    def test_automatic_rejection_is_excluded_before_selection(self, root, workdir):
        accepted = _make_jsonl(root / "a" / "accepted.jsonl", cwd=workdir)
        rejected = _make_jsonl(root / "b" / "rejected.jsonl", cwd=workdir)
        os.utime(accepted, ns=(1, 1))
        os.utime(rejected, ns=(2, 2))
        base = _claude_discovery(root)
        disc = GlobDiscovery(
            root=base.root,
            glob_pattern=base.glob_pattern,
            session_id_of=base.session_id_of,
            cwd_of=base.cwd_of,
            is_shape=base.is_shape,
            birthtime_of=base.birthtime_of,
            loss_probes=base.loss_probes,
            collision_warning=base.collision_warning,
            automatic_rejection_of=lambda p: "not autonomous" if p == rejected else None,
        )

        assert disc.find_transcript(cwd=workdir) == accepted

    def test_after_floor_filters(self, root, workdir):
        _make_jsonl(root / "a" / "old.jsonl", cwd=workdir)
        disc = _claude_discovery(root)
        far_future = time.time() + 10**9
        assert disc.find_transcript(cwd=workdir, after=far_future) is None


class TestScreenTail:
    def test_skip_blank(self):
        capture = "a\n\n  \nb\n"
        assert screen_tail(capture, 2) == ["a", "b"]

    def test_keep_blank(self):
        capture = "a\n\nb\n"
        assert screen_tail(capture, 2, skip_blank=False) == ["", "b"]

    def test_fewer_than_n(self):
        assert screen_tail("a\nb\n", 5) == ["a", "b"]

    def test_empty(self):
        assert screen_tail("", 5) == []

    def test_n_zero_returns_empty(self):
        assert screen_tail("a\nb\n", 0) == []

    def test_n_negative_returns_empty(self):
        assert screen_tail("a\nb\n", -1) == []


class TestBirthtimeHelpers:
    def test_stat_birthtime(self, tmp_path):
        path = tmp_path / "x.txt"
        path.write_text("x")
        st = path.stat()
        result = stat_birthtime(path, st)
        assert result == getattr(st, "st_birthtime", st.st_ctime)

    def test_parent_birthtime(self, tmp_path):
        path = tmp_path / "sub" / "x.txt"
        path.parent.mkdir()
        path.write_text("x")
        st = path.stat()
        result = parent_birthtime(path, st)
        parent_st = path.parent.stat()
        assert result == getattr(parent_st, "st_birthtime", parent_st.st_ctime)

    def test_parent_birthtime_fallback(self, tmp_path):
        path = tmp_path / "nonexistent_parent" / "x.txt"
        st = tmp_path.stat()
        result = parent_birthtime(path, st)
        assert result == st.st_ctime
