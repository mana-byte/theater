from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest
from shipped import ClaudeCodeObserver, CodexObserver, OpenCodeObserver, VibeHarness, VibeObserver

from theater.daemon import methods
from theater.daemon.registry import Registry
from theater.harness import HARNESSES
from theater.harness.builtin.plugins.vibe.constants import ISOLATION_MARKER
from theater.harness.builtin.plugins.vibe.isolation import isolation_marker_text
from theater.models import BadRequest
from theater.transcript_identity import TRANSCRIPT_IDENTITY_LOST_CODE

OPENCODE_SCHEMA = """
CREATE TABLE session (
    id TEXT PRIMARY KEY, parent_id TEXT, directory TEXT,
    time_created INTEGER, time_updated INTEGER
);
"""


def _vibe_session(root: Path, short: str, cwd: Path, *, text: str = "hello") -> Path:
    d = root / f"session_20260816_191459_{short}"
    d.mkdir(parents=True)
    (d / "meta.json").write_text(
        json.dumps(
            {
                "session_id": f"{short}-1111-2222-3333",
                "environment": {"working_directory": str(cwd)},
            }
        ),
        encoding="utf-8",
    )
    messages = d / "messages.jsonl"
    messages.write_text(json.dumps({"role": "assistant", "content": text}) + "\n")
    return messages


def _claude_transcript(root: Path, sid: str, cwd: Path, *, text: str = "hello") -> Path:
    d = root / "project"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{sid}.jsonl"
    path.write_text(
        json.dumps(
            {
                "type": "assistant",
                "cwd": str(cwd),
                "sessionId": sid,
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": text}],
                    "stop_reason": "end_turn",
                    "id": "msg_1",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def _codex_rollout(root: Path, sid: str, cwd: Path, *, text: str = "hello") -> Path:
    d = root / "2026" / "08" / "17"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"rollout-2026-08-17T12-00-00-{sid}.jsonl"
    path.write_text(
        json.dumps(
            {
                "timestamp": "2026-08-17T12:00:00.000Z",
                "type": "session_meta",
                "payload": {"session_id": sid, "cwd": str(cwd)},
            }
        )
        + "\n"
        + json.dumps(
            {
                "timestamp": "2026-08-17T12:00:01.000Z",
                "type": "event_msg",
                "payload": {"type": "agent_message", "message": text},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def _opencode_db(path: Path, sid: str, cwd: Path, *, created: int = 1000) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(OPENCODE_SCHEMA)
        conn.execute(
            "INSERT INTO session (id, parent_id, directory, time_created) VALUES (?, NULL, ?, ?)",
            (sid, str(cwd.resolve()), created),
        )
        conn.commit()
    finally:
        conn.close()


class _OperatorObserver:
    def __init__(self) -> None:
        self.reset: list[str] = []
        self.bound: list[tuple[str, str, str | None]] = []

    async def reset_for_operator_bind(self, pid: str) -> None:
        self.reset.append(pid)

    def record_operator_binding(
        self,
        pid: str,
        location: str,
        session_id: str | None,
        *,
        prior_owner: str | None = None,
    ) -> None:
        self.bound.append((pid, location, session_id))

    def history_is_ambiguous(self, pid, history) -> bool:
        return False


def _daemon(registry: Registry, observer: _OperatorObserver | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        registry=registry,
        store=registry.store,
        observer=observer or _OperatorObserver(),
    )


def test_older_candidate_remains_listed_after_newer_foreign_file(tmp_path):
    root = tmp_path / "vibe"
    root.mkdir()
    project = tmp_path / "project"
    other = tmp_path / "other"
    project.mkdir()
    other.mkdir()
    old = _vibe_session(root, "old00001", project, text="SECRET-OLD")
    _vibe_session(root, "new00001", other, text="foreign")

    rows = VibeObserver(root=root).transcript_candidates(cwd=str(project))

    locations = {row.location for row in rows}
    assert str(old) in locations
    assert "SECRET-OLD" not in json.dumps([asdict(row) for row in rows], default=str)
    assert any(row.rejection_reason == "cwd mismatch" for row in rows)


def test_vibe_isolated_candidate_enumeration_uses_participant_domain(
    registry: Registry, tmp_path, monkeypatch
):
    global_root = tmp_path / "global-vibe"
    isolated = tmp_path / "isolated-vibe"
    global_root.mkdir()
    isolated.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    global_candidate = _vibe_session(global_root, "global01", project, text="GLOBAL")
    isolated_candidate = _vibe_session(isolated, "isolate1", project, text="ISOLATED")
    p = registry.register(harness="vibe", pane="%1", cwd=str(project))
    (isolated / ISOLATION_MARKER).write_text(
        isolation_marker_text(participant_id=p.id, transcript_domain=isolated),
        encoding="utf-8",
    )
    p.transcript_domain = str(isolated.resolve())
    registry.store.upsert_participant(p)
    monkeypatch.setitem(HARNESSES, "vibe", VibeHarness(root=global_root))

    result = asyncio.run(methods._transcript_candidates(_daemon(registry), {"id": p.id}))
    locations = {row["location"] for row in result["candidates"]}

    assert str(isolated_candidate.resolve()) in locations
    assert str(global_candidate.resolve()) not in locations
    assert "ISOLATED" not in json.dumps(result)
    assert all(row["owner"] is None and row["tombstone"] is None for row in result["candidates"])
    with pytest.raises(BadRequest, match="outside"):
        asyncio.run(
            methods._transcript_bind(
                _daemon(registry),
                {"id": p.id, "candidate": str(global_candidate), "confirm_id": p.id},
            )
        )


def test_operator_candidate_validation_rejects_bad_paths(tmp_path):
    root = tmp_path / "vibe"
    root.mkdir()
    project = tmp_path / "project"
    other = tmp_path / "other"
    project.mkdir()
    other.mkdir()
    observer = VibeObserver(root=root)

    good = _vibe_session(root, "good0001", project)
    outside = _vibe_session(tmp_path / "outside", "out00001", project)
    foreign = _vibe_session(root, "badcwd01", other)
    wrong_shape = root / "not-a-session.txt"
    wrong_shape.write_text("{}", encoding="utf-8")
    symlink = root / "session_20260816_191459_link0001"
    symlink.mkdir()
    (symlink / "messages.jsonl").symlink_to(good)

    with pytest.raises(ValueError, match="outside"):
        observer.admit_operator_candidate(cwd=str(project), candidate=str(outside))
    with pytest.raises(ValueError, match="symlink"):
        observer.admit_operator_candidate(
            cwd=str(project), candidate=str(symlink / "messages.jsonl")
        )
    with pytest.raises(ValueError, match="cwd mismatch"):
        observer.admit_operator_candidate(cwd=str(project), candidate=str(foreign))
    with pytest.raises(ValueError, match="harness shape"):
        observer.admit_operator_candidate(cwd=str(project), candidate=str(wrong_shape))
    with pytest.raises(ValueError, match="created before participant floor"):
        observer.admit_operator_candidate(cwd=str(project), candidate=str(good), after=10**12)


def test_claude_bind_admission_validates_shape_cwd_domain_and_floor(tmp_path):
    root = tmp_path / "claude"
    root.mkdir()
    project = tmp_path / "project"
    other = tmp_path / "other"
    project.mkdir()
    other.mkdir()
    observer = ClaudeCodeObserver(root=root)
    good = _claude_transcript(root, "claude-session", project)
    foreign = _claude_transcript(root, "foreign-session", other)
    outside = _claude_transcript(tmp_path / "outside-claude", "outside-session", project)

    row = observer.admit_operator_candidate(cwd=str(project), candidate=str(good))
    assert row.session_id == "claude-session"
    with pytest.raises(ValueError, match="cwd mismatch"):
        observer.admit_operator_candidate(cwd=str(project), candidate=str(foreign))
    with pytest.raises(ValueError, match="outside"):
        observer.admit_operator_candidate(cwd=str(project), candidate=str(outside))
    with pytest.raises(ValueError, match="created before participant floor"):
        observer.admit_operator_candidate(cwd=str(project), candidate=str(good), after=10**12)


def test_claude_receipt_bound_candidate_conflicts_with_operator_bind(
    registry: Registry, tmp_path, monkeypatch
):
    root = tmp_path / "claude"
    root.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    candidate = _claude_transcript(root, "receipt-owned", project)
    monkeypatch.setitem(
        HARNESSES,
        "claude",
        SimpleNamespace(observer=ClaudeCodeObserver(root=root)),
    )
    owner = registry.register(harness="claude", pane="%1", cwd=str(project))
    target = registry.register(harness="claude", pane="%2", cwd=str(project))
    registry.store.record_transcript_receipt(
        owner.id,
        session_id="receipt-owned",
        transcript_location=str(candidate.resolve()),
    )

    rows = asyncio.run(methods._transcript_candidates(_daemon(registry), {"id": target.id}))
    found = next(row for row in rows["candidates"] if row["location"] == str(candidate.resolve()))
    assert found["owner"] == owner.id

    with pytest.raises(BadRequest, match="already owned"):
        asyncio.run(
            methods._transcript_bind(
                _daemon(registry),
                {"id": target.id, "candidate": str(candidate), "confirm_id": target.id},
            )
        )


def test_codex_bind_admission_validates_shape_cwd_domain_and_floor(tmp_path):
    root = tmp_path / "codex"
    root.mkdir()
    project = tmp_path / "project"
    other = tmp_path / "other"
    project.mkdir()
    other.mkdir()
    observer = CodexObserver(root=root)
    good = _codex_rollout(root, "019ff5c6-717c-7a70-9ec4-66dd1f4d173e", project)
    foreign = _codex_rollout(root, "119ff5c6-717c-7a70-9ec4-66dd1f4d173e", other)
    wrong_dir = root / "rollout-2026-08-17T12-00-00-219ff5c6-717c-7a70-9ec4-66dd1f4d173e.jsonl"
    wrong_dir.write_text(good.read_text(encoding="utf-8"), encoding="utf-8")

    row = observer.admit_operator_candidate(cwd=str(project), candidate=str(good))
    assert row.session_id == "019ff5c6-717c-7a70-9ec4-66dd1f4d173e"
    with pytest.raises(ValueError, match="cwd mismatch"):
        observer.admit_operator_candidate(cwd=str(project), candidate=str(foreign))
    with pytest.raises(ValueError, match="harness shape"):
        observer.admit_operator_candidate(cwd=str(project), candidate=str(wrong_dir))
    with pytest.raises(ValueError, match="created before participant floor"):
        observer.admit_operator_candidate(cwd=str(project), candidate=str(good), after=10**12)


def test_opencode_bind_admission_validates_session_cwd_domain_and_floor(tmp_path):
    db = tmp_path / "opencode.db"
    project = tmp_path / "project"
    other = tmp_path / "other"
    project.mkdir()
    other.mkdir()
    _opencode_db(db, "opencode-session", project, created=1000)
    observer = OpenCodeObserver(db=db)

    row = observer.admit_operator_candidate(
        cwd=str(project), candidate="opencode://opencode-session"
    )
    assert row.session_id == "opencode-session"
    with pytest.raises(ValueError, match="cwd mismatch"):
        observer.admit_operator_candidate(cwd=str(other), candidate="opencode://opencode-session")
    with pytest.raises(ValueError, match="harness shape"):
        observer.admit_operator_candidate(cwd=str(project), candidate="opencode://missing")
    with pytest.raises(ValueError, match="outside"):
        observer.admit_operator_candidate(
            cwd=str(project),
            candidate="opencode://opencode-session",
            domain="opencode://elsewhere",
        )
    with pytest.raises(ValueError, match="created before participant floor"):
        observer.admit_operator_candidate(
            cwd=str(project), candidate="opencode://opencode-session", after=2.0
        )


def test_live_and_dead_owner_conflicts_require_exact_transfer(
    registry: Registry, tmp_path, monkeypatch
):
    root = tmp_path / "vibe"
    root.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    candidate = _vibe_session(root, "bind0001", project)
    monkeypatch.setitem(HARNESSES, "vibe", VibeHarness(root=root))
    owner = registry.register(harness="vibe", pane="%1", cwd=str(project))
    target = registry.register(harness="vibe", pane="%2", cwd=str(project))
    daemon = _daemon(registry)

    asyncio.run(
        methods._transcript_bind(
            daemon,
            {"id": owner.id, "candidate": str(candidate), "confirm_id": owner.id},
        )
    )
    with pytest.raises(BadRequest, match="already owned"):
        asyncio.run(
            methods._transcript_bind(
                daemon,
                {"id": target.id, "candidate": str(candidate), "confirm_id": target.id},
            )
        )

    registry.mark_dead(owner.id)
    with pytest.raises(BadRequest, match="already owned"):
        asyncio.run(
            methods._transcript_bind(
                daemon,
                {"id": target.id, "candidate": str(candidate), "confirm_id": target.id},
            )
        )
    result = asyncio.run(
        methods._transcript_bind(
            daemon,
            {
                "id": target.id,
                "candidate": str(candidate),
                "confirm_id": target.id,
                "transfer_from": owner.id,
                "transfer_confirm_id": owner.id,
            },
        )
    )
    assert result["prior_owner"] == owner.id
    assert registry.store.get_participant(target.id).session_correlation == "operator"
    assert daemon.observer.reset[-2:] == [owner.id, target.id]
    events = registry.store.bus_tail(limit=2)
    assert [event["kind"] for event in events] == [
        "operator.transcript_unbind",
        "operator.transcript_bind",
    ]
    assert events[-1]["payload"]["prior_owner"] == owner.id
    assert events[0]["payload"]["transferred_to"] == target.id
    assert not registry.store.observation_error_active(owner.id, TRANSCRIPT_IDENTITY_LOST_CODE)


def test_store_operator_bind_rolls_back_transfer_target_and_audit_on_failure(
    registry: Registry,
):
    owner = registry.register(harness="vibe", pane="%1", cwd="/tmp/project")
    target = registry.register(harness="vibe", pane="%2", cwd="/tmp/project")
    owner.transcript_location = "/tmp/transcript.jsonl"
    owner.session_id = "old-session"
    owner.session_correlation = "operator"
    registry.store.upsert_participant(owner)

    target.transcript_location = "/tmp/transcript.jsonl"
    target.session_id = "new-session"
    target.session_correlation = "operator"
    with pytest.raises(TypeError):
        registry.store.bind_operator_transcript(
            target=target,
            prior_owner=owner,
            audit_payload={"cannot_json": object()},
        )

    kept_owner = registry.store.get_participant(owner.id)
    kept_target = registry.store.get_participant(target.id)
    assert kept_owner.transcript_location == "/tmp/transcript.jsonl"
    assert kept_owner.session_id == "old-session"
    assert kept_owner.session_correlation == "operator"
    assert kept_target.transcript_location is None
    assert kept_target.session_id is None
    assert all(row["kind"] != "operator.transcript_bind" for row in registry.store.bus_tail())


def test_daemon_bind_does_not_update_memory_when_atomic_store_write_fails(
    registry: Registry, tmp_path, monkeypatch
):
    root = tmp_path / "vibe"
    root.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    candidate = _vibe_session(root, "atomic01", project)
    monkeypatch.setitem(HARNESSES, "vibe", VibeHarness(root=root))
    owner = registry.register(harness="vibe", pane="%1", cwd=str(project))
    target = registry.register(harness="vibe", pane="%2", cwd=str(project))
    owner.transcript_location = str(candidate.resolve())
    owner.session_id = "atomic01-1111-2222-3333"
    owner.session_correlation = "operator"
    registry.store.upsert_participant(owner)
    observer = _OperatorObserver()

    def fail_bind_operator_transcript(**_kwargs):
        raise RuntimeError("write failed")

    monkeypatch.setattr(registry.store, "bind_operator_transcript", fail_bind_operator_transcript)
    with pytest.raises(RuntimeError, match="write failed"):
        asyncio.run(
            methods._transcript_bind(
                _daemon(registry, observer),
                {
                    "id": target.id,
                    "candidate": str(candidate),
                    "confirm_id": target.id,
                    "transfer_from": owner.id,
                    "transfer_confirm_id": owner.id,
                },
            )
        )

    kept_owner = registry.store.get_participant(owner.id)
    kept_target = registry.store.get_participant(target.id)
    assert kept_owner.transcript_location == str(candidate.resolve())
    assert kept_target.transcript_location is None
    assert observer.bound == []


def test_bind_persists_operator_and_read_transcript_uses_bound_path(
    registry: Registry, tmp_path, monkeypatch
):
    root = tmp_path / "vibe"
    root.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    bound = _vibe_session(root, "bound001", project, text="BOUND-TEXT")
    _vibe_session(root, "newer001", project, text="NEWER-TEXT")
    monkeypatch.setitem(HARNESSES, "vibe", VibeHarness(root=root))
    p = registry.register(harness="vibe", pane="%1", cwd=str(project))
    daemon = _daemon(registry)

    asyncio.run(
        methods._transcript_bind(
            daemon,
            {"id": p.id, "candidate": str(bound), "confirm_id": p.id},
        )
    )
    restarted = _daemon(Registry(registry.store))
    history = asyncio.run(methods._read_transcript(restarted, {"id": p.id, "last_n": 0}))

    assert history["path"] == str(bound.resolve())
    assert any(event["text"] == "BOUND-TEXT" for event in history["events"])
    assert "NEWER-TEXT" not in json.dumps(history)


def test_bind_does_not_persist_operator_after_rejected_candidate(
    registry: Registry, tmp_path, monkeypatch
):
    root = tmp_path / "vibe"
    root.mkdir()
    project = tmp_path / "project"
    other = tmp_path / "other"
    project.mkdir()
    other.mkdir()
    foreign = _vibe_session(root, "foreign1", other)
    monkeypatch.setitem(HARNESSES, "vibe", VibeHarness(root=root))
    p = registry.register(harness="vibe", pane="%1", cwd=str(project))

    with pytest.raises(BadRequest, match="cwd mismatch"):
        asyncio.run(
            methods._transcript_bind(
                _daemon(registry),
                {"id": p.id, "candidate": str(foreign), "confirm_id": p.id},
            )
        )
    stored = registry.store.get_participant(p.id)
    assert stored.session_correlation is None
    assert stored.transcript_location is None


def test_bind_applies_spawned_creation_floor(registry: Registry, tmp_path, monkeypatch):
    root = tmp_path / "vibe"
    root.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    old = _vibe_session(root, "oldfloor", project)
    monkeypatch.setitem(HARNESSES, "vibe", VibeHarness(root=root))
    time.sleep(0.02)
    p = registry.create_spawned(harness="vibe", cwd=str(project))

    with pytest.raises(BadRequest, match="created before participant floor"):
        asyncio.run(
            methods._transcript_bind(
                _daemon(registry),
                {"id": p.id, "candidate": str(old), "confirm_id": p.id},
            )
        )
    stored = registry.store.get_participant(p.id)
    assert stored.session_correlation is None
    assert stored.transcript_location is None


def test_bind_reaffirms_spawned_trusted_transcript_before_creation_floor(
    registry: Registry, tmp_path, monkeypatch
):
    root = tmp_path / "codex"
    root.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    session_id = "019ff5c6-717c-7a70-9ec4-66dd1f4d173e"
    transcript = _codex_rollout(root, session_id, project)
    monkeypatch.setitem(
        HARNESSES,
        "codex",
        SimpleNamespace(observer=CodexObserver(root=root)),
    )
    time.sleep(0.02)
    participant = registry.create_spawned(harness="codex", cwd=str(project))
    participant.transcript_location = str(transcript.resolve())
    participant.transcript_domain = str(root.resolve())
    participant.session_id = session_id
    participant.session_correlation = "exact"
    registry.store.upsert_participant(participant)
    daemon = _daemon(registry)

    result = asyncio.run(
        methods._transcript_bind(
            daemon,
            {
                "id": participant.id,
                "candidate": str(transcript),
                "confirm_id": participant.id,
            },
        )
    )

    assert result["location"] == str(transcript.resolve())
    assert result["session_id"] == session_id
    assert daemon.observer.reset == [participant.id]
    stored = registry.store.get_participant(participant.id)
    assert stored.session_correlation == "operator"


def test_bind_rpc_requires_confirmation(registry: Registry, tmp_path, monkeypatch):
    root = tmp_path / "vibe"
    root.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    candidate = _vibe_session(root, "confirm1", project)
    monkeypatch.setitem(HARNESSES, "vibe", VibeHarness(root=root))
    p = registry.register(harness="vibe", pane="%1", cwd=str(project))

    with pytest.raises(BadRequest, match="confirm_id"):
        asyncio.run(
            methods._transcript_bind(
                _daemon(registry),
                {"id": p.id, "candidate": str(candidate), "confirm_id": "wrong"},
            )
        )
