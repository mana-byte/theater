from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest
from shipped import VibeHarness, VibeObserver

from theater.daemon import methods
from theater.daemon.registry import Registry
from theater.harness import HARNESSES
from theater.models import BadRequest


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


class _OperatorObserver:
    def __init__(self) -> None:
        self.reset: list[str] = []
        self.bound: list[tuple[str, str, str | None]] = []

    async def reset_for_operator_bind(self, pid: str) -> None:
        self.reset.append(pid)

    def record_operator_binding(self, pid: str, location: str, session_id: str | None) -> None:
        self.bound.append((pid, location, session_id))

    def history_is_ambiguous(self, pid, history) -> bool:
        return False


def _daemon(registry: Registry) -> SimpleNamespace:
    return SimpleNamespace(
        registry=registry,
        store=registry.store,
        observer=_OperatorObserver(),
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
    events = registry.store.bus_tail(limit=1)
    assert events[0]["kind"] == "operator.transcript_bind"
    assert events[0]["payload"]["prior_owner"] == owner.id


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
