"""Resume domain checks per harness: Claude transcript cwd, Codex and OpenCode refusal."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support.launch_preparation import prepare_provider_reservation
from tests.support.resume_harnesses import trusted_predecessor
from tests.support.shipped import ClaudeCodeHarness
from theater.daemon.spawner import Spawner, SpawnRequest
from theater.harness import HARNESSES
from theater.models import BadRequest


async def test_claude_refuses_mismatched_domain(registry, monkeypatch):
    """Claude refuses a predecessor whose domain does not match its
    native observation namespace."""
    monkeypatch.setattr("theater.daemon.spawning.service.shutil.which", lambda b: f"/usr/bin/{b}")
    trusted_predecessor(registry, harness="claude", transcript_domain="/tmp/wrong-claude-root")
    spawner = Spawner(registry)
    req = SpawnRequest(
        harness="claude",
        prompt="",
        cwd="/tmp",
        approval="edits",
        resume="sess-abc",
    )
    with pytest.raises(BadRequest, match="does not match the Claude observation root"):
        await prepare_provider_reservation(spawner, req)


async def test_claude_resume_uses_latest_transcript_project_cwd(
    registry, monkeypatch, terminal_provider, tmp_path
):
    monkeypatch.setattr("theater.daemon.spawning.service.shutil.which", lambda b: f"/usr/bin/{b}")
    root = tmp_path / ".claude" / "projects"
    original = tmp_path / "Desktop"
    current = original / "sre-infra"
    original.mkdir()
    current.mkdir()
    session_id = "f2c02c06-8864-4144-bd87-36a0f9cd33dd"
    transcript = root / "-Users-manaiki-laut-Desktop-sre-infra" / f"{session_id}.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(
        "\n".join(
            json.dumps(record)
            for record in (
                {"sessionId": session_id, "cwd": str(original)},
                {"sessionId": session_id, "cwd": str(current)},
            )
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setitem(HARNESSES, "claude", ClaudeCodeHarness(root=root))
    predecessor = trusted_predecessor(registry, harness="claude", session_id=session_id)
    predecessor.cwd = str(original)
    predecessor.transcript_location = str(transcript)
    registry.store.upsert_participant(predecessor)
    registry.mark_dead(predecessor.id)

    reservation = await prepare_provider_reservation(
        Spawner(registry),
        SpawnRequest(
            harness="claude",
            prompt="continue",
            cwd=str(original),
            approval="manual",
            resume=session_id,
        ),
    )

    successor = reservation.participant
    command = reservation.plan.argv
    assert reservation.child_cwd == str(current)
    assert successor.cwd == str(current)
    assert f"--resume={session_id}" in command
    assert "--fork-session" in command
    fresh = next(arg for arg in command if arg.startswith("--session-id="))
    assert fresh != f"--session-id={session_id}"


async def test_claude_resume_requires_a_materialized_native_transcript(
    registry, monkeypatch, terminal_provider, tmp_path
):
    from theater.daemon.rpc.participants import _resume_state

    monkeypatch.setattr("theater.daemon.spawning.service.shutil.which", lambda b: f"/usr/bin/{b}")
    root = tmp_path / ".claude" / "projects"
    root.mkdir(parents=True)
    session_id = "f2c02c06-8864-4144-bd87-36a0f9cd33dd"
    monkeypatch.setitem(HARNESSES, "claude", ClaudeCodeHarness(root=root))
    predecessor = trusted_predecessor(registry, harness="claude", session_id=session_id)

    assert _resume_state(predecessor, []) == "harness_resume_rejected"
    with pytest.raises(BadRequest, match="native transcript has not materialized"):
        await prepare_provider_reservation(
            Spawner(registry),
            SpawnRequest(
                harness="claude",
                prompt="",
                cwd=str(tmp_path),
                approval="manual",
                resume=session_id,
            ),
        )

    assert [p.id for p in registry.list(include_dead=True)] == [predecessor.id]
    assert terminal_provider.creations == []


def test_claude_resume_state_preflight_does_not_read_transcript(registry, monkeypatch, tmp_path):
    from theater.daemon.rpc.participants import _resume_state

    root = tmp_path / ".claude" / "projects"
    transcript = root / "-repo" / "sess-abc.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("not JSON\n", encoding="utf-8")
    monkeypatch.setitem(HARNESSES, "claude", ClaudeCodeHarness(root=root))
    predecessor = trusted_predecessor(registry, harness="claude")
    predecessor.transcript_location = str(transcript)
    registry.store.upsert_participant(predecessor)
    registry.mark_dead(predecessor.id)

    def no_read(*args, **kwargs):
        raise AssertionError("resume-state preflight read transcript contents")

    monkeypatch.setattr(Path, "open", no_read)
    assert _resume_state(registry.get(predecessor.id), []) == "resumable"


def test_claude_resume_cwd_uses_latest_bounded_suffix_record(monkeypatch, tmp_path):
    from theater.harness.builtin.plugins.claude import identity

    old = tmp_path / "old"
    current = tmp_path / "current"
    old.mkdir()
    current.mkdir()
    session_id = "f2c02c06-8864-4144-bd87-36a0f9cd33dd"
    transcript = tmp_path / f"{session_id}.jsonl"
    records = [json.dumps({"session_id": session_id, "cwd": str(old)})]
    records.extend(json.dumps({"session_id": session_id, "padding": "x" * 128}) for _ in range(16))
    records.append(json.dumps({"sessionId": session_id, "cwd": str(current)}))
    transcript.write_text("\n".join(records) + "\n", encoding="utf-8")
    monkeypatch.setattr(identity, "_RESUME_CWD_SUFFIX_BYTES", 512)
    monkeypatch.setattr(identity, "_RESUME_CWD_SUFFIX_RECORDS", 8)

    assert identity._current_transcript_cwd(transcript, session_id) == str(current)


def test_claude_resume_cwd_accepts_final_record_without_newline(tmp_path):
    from theater.harness.builtin.plugins.claude import identity

    cwd = tmp_path / "current"
    cwd.mkdir()
    session_id = "f2c02c06-8864-4144-bd87-36a0f9cd33dd"
    transcript = tmp_path / f"{session_id}.jsonl"
    transcript.write_text(
        json.dumps({"sessionId": session_id, "cwd": str(cwd)}),
        encoding="utf-8",
    )

    assert identity._current_transcript_cwd(transcript, session_id) == str(cwd)


@pytest.mark.parametrize("tail", ['{"incomplete"', json.dumps({"sessionId": "sess-abc"})])
def test_claude_resume_cwd_ignores_unusable_final_record(tmp_path, tail):
    from theater.harness.builtin.plugins.claude import identity

    cwd = tmp_path / "current"
    cwd.mkdir()
    session_id = "sess-abc"
    transcript = tmp_path / f"{session_id}.jsonl"
    transcript.write_text(
        json.dumps({"sessionId": session_id, "cwd": str(cwd)}) + "\n" + tail,
        encoding="utf-8",
    )

    assert identity._current_transcript_cwd(transcript, session_id) == str(cwd)


def test_claude_resume_cwd_refuses_latest_relative_cwd(tmp_path):
    from theater.harness.builtin.plugins.claude import identity

    old = tmp_path / "old"
    old.mkdir()
    session_id = "sess-abc"
    transcript = tmp_path / f"{session_id}.jsonl"
    transcript.write_text(
        "\n".join(
            (
                json.dumps({"sessionId": session_id, "cwd": str(old)}),
                json.dumps({"sessionId": session_id, "cwd": "relative"}),
            )
        ),
        encoding="utf-8",
    )

    with pytest.raises(BadRequest, match="records a relative cwd"):
        identity._current_transcript_cwd(transcript, session_id)


def test_claude_resume_transcript_refuses_duplicate_known_location(tmp_path):
    from theater.harness.builtin.plugins.claude import identity

    root = tmp_path / ".claude" / "projects"
    session_id = "sess-abc"
    first = root / "-first" / f"{session_id}.jsonl"
    second = root / "-second" / f"{session_id}.jsonl"
    first.parent.mkdir(parents=True)
    second.parent.mkdir()
    first.write_text("", encoding="utf-8")
    second.write_text("", encoding="utf-8")

    with pytest.raises(BadRequest, match="multiple native transcripts"):
        identity.materialized_resume_transcript(
            root=root,
            session_id=session_id,
            known_location=str(first),
        )


def test_vibe_resume_state_remains_generic(registry):
    from theater.daemon.rpc.participants import _resume_state

    predecessor = trusted_predecessor(registry, harness="vibe")
    assert _resume_state(predecessor, []) == "resumable"


async def test_claude_resume_keeps_a_matching_transcript_cwd(
    registry, monkeypatch, terminal_provider, tmp_path
):
    monkeypatch.setattr("theater.daemon.spawning.service.shutil.which", lambda b: f"/usr/bin/{b}")
    root = tmp_path / ".claude" / "projects"
    cwd = tmp_path / "repo"
    cwd.mkdir()
    session_id = "f2c02c06-8864-4144-bd87-36a0f9cd33dd"
    transcript = root / "-repo" / f"{session_id}.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(
        json.dumps({"session_id": session_id, "cwd": str(cwd)}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setitem(HARNESSES, "claude", ClaudeCodeHarness(root=root))
    predecessor = trusted_predecessor(registry, harness="claude", session_id=session_id)
    predecessor.cwd = str(cwd)
    predecessor.transcript_location = str(transcript)
    registry.store.upsert_participant(predecessor)
    registry.mark_dead(predecessor.id)

    reservation = await prepare_provider_reservation(
        Spawner(registry),
        SpawnRequest(
            harness="claude",
            prompt="",
            cwd=str(cwd),
            approval="manual",
            resume=session_id,
        ),
    )

    successor = reservation.participant
    assert reservation.child_cwd == str(cwd)
    assert successor.cwd == str(cwd)


async def test_codex_refuses_mismatched_domain(registry, monkeypatch):
    """Codex refuses a predecessor whose domain does not match its
    native observation namespace."""
    monkeypatch.setattr("theater.daemon.spawning.service.shutil.which", lambda b: f"/usr/bin/{b}")
    trusted_predecessor(registry, harness="codex", transcript_domain="/tmp/wrong-codex-root")
    spawner = Spawner(registry)
    req = SpawnRequest(
        harness="codex",
        prompt="",
        cwd="/tmp",
        approval="edits",
        resume="sess-abc",
    )
    with pytest.raises(BadRequest, match="does not match the Codex observation root"):
        await prepare_provider_reservation(spawner, req)


async def test_opencode_refuses_a_foreign_database_lineage(registry, monkeypatch, tmp_path):
    """A trusted participant cannot make a database outside Theater resumable."""
    from theater.harness.builtin.plugins.opencode.constants import DB_NAME
    from theater.harness.builtin.plugins.opencode.dialect import domain_for

    monkeypatch.setattr("theater.daemon.spawning.service.shutil.which", lambda b: f"/usr/bin/{b}")
    trusted_predecessor(
        registry, harness="opencode", transcript_domain=domain_for(tmp_path / DB_NAME)
    )
    spawner = Spawner(registry)
    req = SpawnRequest(
        harness="opencode",
        prompt="",
        cwd="/tmp",
        approval="edits",
        resume="sess-abc",
    )
    with pytest.raises(BadRequest, match="no supported isolated database lineage"):
        await prepare_provider_reservation(spawner, req)
