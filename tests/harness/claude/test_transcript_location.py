"""Claude transcript discovery: session files, cwd matching, children and the config root."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from theater.harness.builtin.plugins.claude.identity import (
    authoritative_resume_cwd,
    materialized_resume_transcript,
)
from theater.harness.builtin.plugins.claude.launch import (
    resume_launch_overlay,
    resume_preflight,
)
from theater.harness.builtin.plugins.claude.observer import ClaudeCodeObserver
from theater.harness.contracts.callbacks import ResumeContext, ResumePreflightContext
from theater.models import BadRequest, Participant

CONFIG_ROOT_SESSION = "11111111-1111-4111-8111-111111111111"


def _transcript(root: Path, session_id: str, cwd: Path) -> Path:
    path = root / "project" / f"{session_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"session_id": session_id, "cwd": str(cwd)}) + "\n")
    return path


def _predecessor(root: Path, transcript: Path) -> Participant:
    return Participant(
        harness="claude",
        session_id=CONFIG_ROOT_SESSION,
        transcript_domain=str(root.resolve()),
        transcript_location=str(transcript),
    )


def _receipt(observer: ClaudeCodeObserver, transcript: Path):
    return observer.validate_transcript_receipt(
        payload={"session_id": CONFIG_ROOT_SESSION, "transcript_path": str(transcript)},
        cwd=None,
        expected_session_id=CONFIG_ROOT_SESSION,
    )


def test_custom_config_root_transcript_accepted_and_resume_finds_it(tmp_path, monkeypatch) -> None:
    config = tmp_path / "claude-config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    root = config / "projects"
    transcript = _transcript(root, CONFIG_ROOT_SESSION, tmp_path)
    predecessor = _predecessor(root, transcript)

    observer = ClaudeCodeObserver()
    assert observer.root == root
    assert _receipt(observer, transcript).location == str(transcript.resolve())

    resume_preflight(ResumePreflightContext(predecessor=predecessor))
    overlay = resume_launch_overlay(
        ResumeContext(predecessor=predecessor, trusted_session_owners=())
    )
    assert overlay.cwd == str(tmp_path.resolve())
    assert overlay.transcript_domain == str(root.resolve())


def test_custom_config_root_still_rejects_transcripts_outside_it(tmp_path, monkeypatch) -> None:
    config = tmp_path / "claude-config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    default_root = home / ".claude" / "projects"
    stray = _transcript(default_root, CONFIG_ROOT_SESSION, tmp_path)

    observer = ClaudeCodeObserver()
    with pytest.raises(ValueError, match="outside Claude's transcript root"):
        _receipt(observer, stray)
    predecessor = _predecessor(default_root, stray)
    with pytest.raises(BadRequest, match="does not match"):
        resume_preflight(ResumePreflightContext(predecessor=predecessor))


def test_default_root_when_env_unset_is_unchanged(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    root = home / ".claude" / "projects"
    transcript = _transcript(root, CONFIG_ROOT_SESSION, tmp_path)
    predecessor = _predecessor(root, transcript)

    observer = ClaudeCodeObserver()
    assert observer.root == root
    assert _receipt(observer, transcript).location == str(transcript.resolve())
    resume_preflight(ResumePreflightContext(predecessor=predecessor))


SESSION = "b67b4276-f8b8-43ed-9987-0b5b3828c8cd"


def write(path: Path, records: list[dict | str]) -> Path:
    """A transcript file, with raw strings passed through unencoded."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [r if isinstance(r, str) else json.dumps(r) for r in records]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_a_missing_projects_directory_is_not_an_error(tmp_path):
    """Claude may simply not be installed; that is a None, not a crash."""
    observer = ClaudeCodeObserver(root=tmp_path / "never-created")
    assert observer.find_transcript(cwd="/tmp/x") is None


def test_a_known_session_id_is_found_by_filename_without_reading_anything(root, observer):
    """The stem *is* the session id, so this needs no scan and no cwd guess."""
    want = write(root / "-tmp-x" / f"{SESSION}.jsonl", [{"cwd": "/somewhere/else"}])
    assert observer.find_transcript(cwd="/tmp/x", session_id=SESSION) == want


def test_resume_escapes_metacharacter_ids_but_discovery_keeps_its_glob(root, observer):
    exact_id = "sess[ab]"
    exact = write(root / "project" / f"{exact_id}.jsonl", [{"cwd": "/elsewhere"}])
    glob_match = write(root / "project" / "sessa.jsonl", [{"cwd": "/elsewhere"}])

    assert (
        materialized_resume_transcript(root=root, session_id=exact_id, known_location=None)
        == exact.resolve()
    )
    assert observer.find_transcript(cwd="/tmp/x", session_id=exact_id) == glob_match


def test_resume_checks_both_session_keys_while_receipt_uses_first(root, observer, tmp_path):
    path = write(
        root / "project" / f"{CONFIG_ROOT_SESSION}.jsonl",
        [{"session_id": CONFIG_ROOT_SESSION, "sessionId": "other", "cwd": str(tmp_path)}],
    )

    assert _receipt(observer, path).location == str(path.resolve())
    with pytest.raises(BadRequest, match="different native session id"):
        authoritative_resume_cwd(
            root=root, session_id=CONFIG_ROOT_SESSION, known_location=str(path)
        )


def test_an_unknown_session_id_falls_back_to_the_cwd_scan(root, observer):
    """A stale id must not shadow a transcript that is really there."""
    want = write(root / "-tmp-x" / "aaa.jsonl", [{"cwd": "/tmp/x"}])
    assert observer.find_transcript(cwd="/tmp/x", session_id="not-on-disk") == want


def test_without_a_cwd_there_is_nothing_to_match_on(observer):
    assert observer.find_transcript(cwd="") is None


def test_the_newest_transcript_wins_among_several_in_one_directory(root, observer):
    """Two agents in one repo differ only by mtime until one of them speaks."""
    old = write(root / "-tmp-x" / "old.jsonl", [{"cwd": "/tmp/x"}])
    new = write(root / "-tmp-x" / "new.jsonl", [{"cwd": "/tmp/x"}])
    import os

    os.utime(old, (1000, 1000))
    os.utime(new, (2000, 2000))
    assert observer.find_transcript(cwd="/tmp/x") == new


def test_a_transcript_older_than_the_participant_is_not_theirs(root, observer):
    """Without this, a respawn in the same repo re-attaches to the dead session."""
    write(root / "-tmp-x" / "aaa.jsonl", [{"cwd": "/tmp/x"}])
    assert observer.find_transcript(cwd="/tmp/x", after=0) is not None
    assert observer.find_transcript(cwd="/tmp/x", after=time.time() + 3600) is None


def test_a_transcript_for_another_directory_is_ignored(root, observer):
    write(root / "-tmp-y" / "aaa.jsonl", [{"cwd": "/tmp/y"}])
    assert observer.find_transcript(cwd="/tmp/x") is None


def test_the_cwd_is_resolved_before_it_is_compared(root, observer, tmp_path):
    """`/tmp` is a symlink to `/private/tmp` on macOS; the two must still match."""
    real = tmp_path / "repo"
    real.mkdir()
    write(root / "-repo" / "aaa.jsonl", [{"cwd": str(real)}])
    assert observer.find_transcript(cwd=f"{real}/.") is not None


def test_a_malformed_line_does_not_hide_the_cwd_on_the_next_one(root, observer):
    """Claude writes partial lines while flushing; one is not a reason to give up."""
    want = write(root / "-tmp-x" / "aaa.jsonl", ['{"cwd": "/tmp/x"', {"cwd": "/tmp/x"}])
    assert observer.find_transcript(cwd="/tmp/x") == want


def test_the_cwd_probe_gives_up_rather_than_read_a_whole_transcript(root, observer):
    """A transcript can be megabytes; the cwd is in the first records or nowhere."""
    filler: list[dict | str] = [{"type": "system"} for _ in range(25)]
    write(root / "-tmp-x" / "aaa.jsonl", [*filler, {"cwd": "/tmp/x"}])
    assert observer.find_transcript(cwd="/tmp/x") is None


def test_a_transcript_that_cannot_be_read_is_skipped_not_fatal(root, observer, monkeypatch):
    write(root / "-tmp-x" / "aaa.jsonl", [{"cwd": "/tmp/x"}])
    original = Path.open

    def deny(self, *a, **kw):
        if self.suffix == ".jsonl":
            raise OSError("permission denied")
        return original(self, *a, **kw)

    monkeypatch.setattr(Path, "open", deny)
    assert observer.find_transcript(cwd="/tmp/x") is None


def test_the_session_id_is_read_off_the_filename(observer):
    assert observer.session_id(Path(f"/x/{SESSION}.jsonl")) == SESSION
