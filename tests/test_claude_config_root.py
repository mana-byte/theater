"""Claude transcript root follows $CLAUDE_CONFIG_DIR.

Observation, receipt validation, and resume must all see the same root, and
containment must not loosen when the root moves.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from theater.harness.builtin.plugins.claude.launch import (
    plan_launch,
    resume_launch_overlay,
    resume_preflight,
)
from theater.harness.builtin.plugins.claude.observer import ClaudeCodeObserver
from theater.harness.contracts.callbacks import LaunchContext, ResumeContext, ResumePreflightContext
from theater.models import BadRequest, Participant

SESSION = "11111111-1111-4111-8111-111111111111"


def _transcript(root: Path, session_id: str, cwd: Path) -> Path:
    path = root / "project" / f"{session_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"session_id": session_id, "cwd": str(cwd)}) + "\n")
    return path


def _predecessor(root: Path, transcript: Path) -> Participant:
    return Participant(
        harness="claude",
        session_id=SESSION,
        transcript_domain=str(root.resolve()),
        transcript_location=str(transcript),
    )


def _receipt(observer: ClaudeCodeObserver, transcript: Path):
    return observer.validate_transcript_receipt(
        payload={"session_id": SESSION, "transcript_path": str(transcript)},
        cwd=None,
        expected_session_id=SESSION,
    )


def test_custom_config_root_transcript_accepted_and_resume_finds_it(tmp_path, monkeypatch) -> None:
    config = tmp_path / "claude-config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    root = config / "projects"
    transcript = _transcript(root, SESSION, tmp_path)
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
    stray = _transcript(default_root, SESSION, tmp_path)

    observer = ClaudeCodeObserver()
    with pytest.raises(ValueError, match="outside Claude's transcript root"):
        _receipt(observer, stray)
    predecessor = _predecessor(default_root, stray)
    with pytest.raises(BadRequest, match="does not match"):
        resume_preflight(ResumePreflightContext(predecessor=predecessor))


def test_plan_env_pins_config_dir_only_when_configured(tmp_path, monkeypatch) -> None:
    def context() -> LaunchContext:
        return LaunchContext(
            participant_id="abc123",
            prompt="hi",
            config_path=tmp_path / "mcp.json",
            approval="manual",
        )

    config = tmp_path / "claude-config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    plan = plan_launch(context())
    assert plan.env["CLAUDE_CONFIG_DIR"] == str(config)

    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    plan = plan_launch(context())
    assert "CLAUDE_CONFIG_DIR" not in plan.env


def test_default_root_when_env_unset_is_unchanged(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    root = home / ".claude" / "projects"
    transcript = _transcript(root, SESSION, tmp_path)
    predecessor = _predecessor(root, transcript)

    observer = ClaudeCodeObserver()
    assert observer.root == root
    assert _receipt(observer, transcript).location == str(transcript.resolve())
    resume_preflight(ResumePreflightContext(predecessor=predecessor))
