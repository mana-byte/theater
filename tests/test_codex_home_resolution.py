"""Codex transcript-root resolution from CODEX_HOME launch/environment evidence.

The failure this covers: the observer hardcoded `~/.codex/sessions`, so a
participant whose Codex state lives under another CODEX_HOME produced no
candidates, no process proof, and a resume domain that never matched. Codex's
own semantics were verified against the real CLI: an empty value means the
default home, and a relative value resolves against the CLI process cwd, which
for a Theater pane is the participant cwd.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from theater import proc
from theater.harness.builtin.plugins.codex.homes import sessions_root
from theater.harness.builtin.plugins.codex.identity import transcript_candidates
from theater.harness.builtin.plugins.codex.launch import plan_launch, resume_launch_overlay
from theater.harness.builtin.plugins.codex.observer import CodexObserver
from theater.harness.builtin.plugins.codex.source import source_for
from theater.harness.contracts.callbacks import (
    LaunchContext,
    ResumeContext,
    TranscriptCandidatesContext,
)
from theater.harness.contracts.context import ParticipantObservationContext
from theater.models import BadRequest, Participant

SESSION = "01a00cdf-17f9-7851-99a4-b0dbaad18bed"
PID = 74065


def _rollout(root: Path, cwd: Path) -> Path:
    day = root / "2026" / "08" / "17"
    day.mkdir(parents=True, exist_ok=True)
    path = day / f"rollout-2026-08-17T01-19-02-{SESSION}.jsonl"
    path.write_text(
        json.dumps(
            {
                "timestamp": "2026-08-17T01:19:02.000Z",
                "type": "session_meta",
                "payload": {"id": SESSION, "cwd": str(cwd)},
            }
        )
        + "\n"
    )
    return path


@pytest.fixture
def participant_cwd(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    return project


def test_without_codex_home_the_default_home_is_used(monkeypatch):
    monkeypatch.delenv("CODEX_HOME", raising=False)
    assert sessions_root() == Path.home() / ".codex" / "sessions"
    assert CodexObserver().root == Path.home() / ".codex" / "sessions"


def test_an_empty_codex_home_is_the_default_home(monkeypatch):
    monkeypatch.setenv("CODEX_HOME", "")
    assert sessions_root(cwd="/somewhere") == Path.home() / ".codex" / "sessions"


def test_an_absolute_codex_home_names_the_sessions_root(monkeypatch, tmp_path):
    home = tmp_path / "state"
    monkeypatch.setenv("CODEX_HOME", str(home))
    assert sessions_root() == home / "sessions"


def test_a_relative_codex_home_resolves_against_the_participant_cwd(monkeypatch, participant_cwd):
    monkeypatch.setenv("CODEX_HOME", "codex-state")
    assert sessions_root(cwd=str(participant_cwd)) == participant_cwd / "codex-state" / "sessions"


def test_an_explicit_root_overrides_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "other"))
    assert CodexObserver(root=tmp_path / "explicit").root == tmp_path / "explicit"


def test_candidates_and_sources_use_the_environment_root(monkeypatch, tmp_path, participant_cwd):
    home = tmp_path / "state"
    monkeypatch.setenv("CODEX_HOME", str(home))
    rollout = _rollout(home / "sessions", participant_cwd)

    rows = transcript_candidates(TranscriptCandidatesContext(cwd=str(participant_cwd), domain=None))

    assert [row.location for row in rows] == [str(rollout)]
    source = source_for(ParticipantObservationContext(participant_id="p", cwd=str(participant_cwd)))
    assert source is not None


def test_process_proof_outside_the_environment_home_is_rejected(
    monkeypatch, tmp_path, participant_cwd
):
    home = tmp_path / "state"
    monkeypatch.setenv("CODEX_HOME", str(home))
    inside = _rollout(home / "sessions", participant_cwd)
    outside = _rollout(tmp_path / "elsewhere" / "sessions", participant_cwd)
    monkeypatch.setattr(proc, "comm", lambda pid: "codex")
    monkeypatch.setattr(proc, "open_files", lambda pid: [outside])

    reader = CodexObserver(pane_pid=PID, participant_scoped=True)
    assert reader.find_transcript(cwd=str(participant_cwd)) is None

    monkeypatch.setattr(proc, "open_files", lambda pid: [inside])
    assert reader.find_transcript(cwd=str(participant_cwd)) == inside.resolve()


def test_plan_launch_pins_the_pane_home_onto_the_launch(monkeypatch, tmp_path):
    home = tmp_path / "state"
    monkeypatch.setenv("CODEX_HOME", str(home))
    context = LaunchContext(
        participant_id="p", prompt="hi", config_path=tmp_path / "c.json", approval="manual"
    )
    assert plan_launch(context).env == {"CODEX_HOME": str(home)}

    # Empty means the default, and an absent value would let the pane inherit
    # the tmux server's possibly different home — both pin the default.
    monkeypatch.setenv("CODEX_HOME", "")
    assert plan_launch(context).env == {"CODEX_HOME": str(Path.home() / ".codex")}
    monkeypatch.delenv("CODEX_HOME", raising=False)
    assert plan_launch(context).env == {"CODEX_HOME": str(Path.home() / ".codex")}


def test_plan_launch_pins_a_relative_home_raw(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_HOME", "codex-state")
    context = LaunchContext(
        participant_id="p", prompt="hi", config_path=tmp_path / "c.json", approval="manual"
    )
    assert plan_launch(context).env == {"CODEX_HOME": "codex-state"}


def test_resume_validates_against_the_environment_root(monkeypatch, tmp_path):
    home = tmp_path / "state"
    monkeypatch.setenv("CODEX_HOME", str(home))
    domain = str((home / "sessions").resolve())
    predecessor = Participant(harness="codex", transcript_domain=domain)
    overlay = resume_launch_overlay(
        ResumeContext(predecessor=predecessor, trusted_session_owners=())
    )
    assert overlay.transcript_domain == domain

    predecessor = Participant(harness="codex", transcript_domain=str(tmp_path / "other"))
    with pytest.raises(BadRequest):
        resume_launch_overlay(ResumeContext(predecessor=predecessor, trusted_session_owners=()))


def test_a_relative_home_resume_is_refused_not_silently_mismatched(monkeypatch, tmp_path):
    """The fork would run in a new cwd and miss the predecessor's home entirely."""
    monkeypatch.setenv("CODEX_HOME", "codex-state")
    predecessor = Participant(
        harness="codex",
        cwd=str(tmp_path / "old-worktree"),
        transcript_domain=str((tmp_path / "old-worktree" / "codex-state" / "sessions").resolve()),
    )
    with pytest.raises(BadRequest, match="relative"):
        resume_launch_overlay(ResumeContext(predecessor=predecessor, trusted_session_owners=()))

    # An explicit root keeps its precedence and is never refused.
    overlay = resume_launch_overlay(
        ResumeContext(predecessor=predecessor, trusted_session_owners=()),
        root=tmp_path / "old-worktree" / "codex-state" / "sessions",
    )
    assert overlay.transcript_domain == str(
        (tmp_path / "old-worktree" / "codex-state" / "sessions").resolve()
    )
