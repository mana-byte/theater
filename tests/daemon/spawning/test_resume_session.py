"""Resume session trust: dead and live bindings, Vibe domains, floor persistence, id references."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.support.resume_harnesses import ResumeHarness, trusted_resume
from theater.daemon.spawner import Spawner, SpawnRequest
from theater.harness import HARNESSES
from theater.harness.builtin.plugins.vibe.constants import ISOLATION_MARKER
from theater.harness.builtin.plugins.vibe.isolation import isolation_marker_text
from theater.models import BadRequest


@pytest.fixture
def resume_harness(monkeypatch):
    h = ResumeHarness()
    monkeypatch.setitem(HARNESSES, "resume-spawn-test", h)
    return h


async def test_resume_refuses_unknown_session_id(registry, resume_harness, monkeypatch):
    monkeypatch.setattr("theater.daemon.spawning.service.shutil.which", lambda b: f"/usr/bin/{b}")
    spawner = Spawner(registry)
    req = SpawnRequest(
        harness="resume-spawn-test",
        prompt="",
        cwd="/tmp",
        approval="edits",
        resume="sess-abc",
    )

    with pytest.raises(BadRequest, match="no trusted"):
        await spawner.reserve(req)


async def test_resume_refuses_heuristic_session_id(registry, resume_harness, monkeypatch):
    monkeypatch.setattr("theater.daemon.spawning.service.shutil.which", lambda b: f"/usr/bin/{b}")
    p = registry.register(
        harness="resume-spawn-test",
        pane=None,
        cwd="/tmp",
        session_id="sess-abc",
    )
    p.session_correlation = "heuristic"
    registry.store.upsert_participant(p)
    spawner = Spawner(registry)
    req = SpawnRequest(
        harness="resume-spawn-test",
        prompt="",
        cwd="/tmp",
        approval="edits",
        resume="sess-abc",
    )

    with pytest.raises(BadRequest, match="no trusted"):
        await spawner.reserve(req)


async def test_resume_allows_trusted_dead_session(registry, resume_harness, monkeypatch):
    monkeypatch.setattr("theater.daemon.spawning.service.shutil.which", lambda b: f"/usr/bin/{b}")
    trusted_resume(registry, harness="resume-spawn-test")
    spawner = Spawner(registry)
    req = SpawnRequest(
        harness="resume-spawn-test",
        prompt="",
        cwd="/tmp",
        approval="edits",
        resume="sess-abc",
    )

    await spawner.reserve(req)

    assert resume_harness.seen_resume == "sess-abc"


async def test_resume_refuses_live_trusted_session_id(registry, resume_harness, monkeypatch):
    monkeypatch.setattr("theater.daemon.spawning.service.shutil.which", lambda b: f"/usr/bin/{b}")
    p = trusted_resume(registry, harness="resume-spawn-test", live=True)
    spawner = Spawner(registry)
    req = SpawnRequest(
        harness="resume-spawn-test",
        prompt="",
        cwd="/tmp",
        approval="edits",
        resume="sess-abc",
    )

    with pytest.raises(BadRequest, match=f"trusted owner {p.id} is still live"):
        await spawner.reserve(req)


async def test_dead_trusted_binding_remains_resumable_when_transcript_is_missing(
    registry, resume_harness, monkeypatch, tmp_path, terminal_provider
):
    monkeypatch.setattr("theater.daemon.spawning.service.shutil.which", lambda b: f"/usr/bin/{b}")
    p = trusted_resume(registry, harness="resume-spawn-test")
    p = registry.get(p.id)
    p.transcript_location = str(tmp_path / "missing" / "messages.jsonl")
    p.transcript_domain = str(tmp_path.resolve())
    registry.store.upsert_participant(p)
    spawner = Spawner(registry)
    req = SpawnRequest(
        harness="resume-spawn-test",
        prompt="",
        cwd="/tmp",
        approval="edits",
        resume="sess-abc",
    )

    spawned = (await spawner.reserve(req)).participant

    assert spawned.status.value == "idle"
    assert resume_harness.seen_resume == "sess-abc"


async def test_vibe_resume_reuses_trusted_isolated_domain(registry, tmp_path, terminal_provider):
    project = tmp_path / "project"
    project.mkdir()
    domain = tmp_path / "isolated-vibe"
    domain.mkdir()
    transcript = domain / "session_20260817_120000_sessabc1" / "messages.jsonl"
    transcript.parent.mkdir()
    transcript.write_text('{"role":"assistant","content":"old"}\n', encoding="utf-8")
    predecessor = registry.register(
        harness="vibe",
        pane=None,
        cwd=str(project),
        session_id="sessabc1-1111-2222-3333",
    )
    (domain / ISOLATION_MARKER).write_text(
        isolation_marker_text(participant_id=predecessor.id, transcript_domain=domain),
        encoding="utf-8",
    )
    predecessor.session_correlation = "operator"
    predecessor.transcript_domain = str(domain.resolve())
    predecessor.transcript_location = str(transcript)
    registry.store.upsert_participant(predecessor)
    registry.mark_dead(predecessor.id)

    reservation = await Spawner(registry).reserve(
        SpawnRequest(
            harness="vibe",
            prompt="continue",
            cwd=str(project),
            approval="manual",
            resume="sessabc1-1111-2222-3333",
        )
    )

    spawned = reservation.participant
    assert reservation.plan.env["VIBE_SESSION_LOGGING__SAVE_DIR"] == str(domain.resolve())
    assert registry.get(spawned.id).transcript_domain == str(domain.resolve())


async def test_vibe_resume_can_repeat_from_successor(registry, tmp_path, terminal_provider):
    project = tmp_path / "project"
    project.mkdir()
    spawner = Spawner(registry)

    cold_reservation = await spawner.reserve(
        SpawnRequest(
            harness="vibe",
            prompt="start",
            cwd=str(project),
            approval="manual",
        )
    )
    cold = registry.get(cold_reservation.participant.id)
    assert cold.transcript_domain is not None
    domain = Path(cold.transcript_domain)
    transcript = domain / "session_20260817_120000_sessabc1" / "messages.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text('{"role":"assistant","content":"cold"}\n', encoding="utf-8")
    cold.session_id = "sessabc1-1111-2222-3333"
    cold.session_correlation = "exact"
    cold.transcript_location = str(transcript)
    registry.store.upsert_participant(cold)
    registry.mark_dead(cold.id)

    first_reservation = await spawner.reserve(
        SpawnRequest(
            harness="vibe",
            prompt="resume once",
            cwd=str(project),
            approval="manual",
            resume="sessabc1-1111-2222-3333",
        )
    )
    first = first_reservation.participant
    registry.mark_dead(first.id)

    second_reservation = await spawner.reserve(
        SpawnRequest(
            harness="vibe",
            prompt="resume twice",
            cwd=str(project),
            approval="manual",
            resume="sessabc1-1111-2222-3333",
        )
    )

    second = second_reservation.participant
    assert first_reservation.plan.env["VIBE_SESSION_LOGGING__SAVE_DIR"] == str(domain)
    assert second_reservation.plan.env["VIBE_SESSION_LOGGING__SAVE_DIR"] == str(domain)
    assert registry.get(first.id).transcript_domain == str(domain)
    assert registry.get(second.id).transcript_domain == str(domain)


async def test_vibe_resume_refuses_live_successor_even_with_dead_lineage(
    registry, tmp_path, terminal_provider
):
    project = tmp_path / "project"
    project.mkdir()
    spawner = Spawner(registry)

    cold_reservation = await spawner.reserve(
        SpawnRequest(
            harness="vibe",
            prompt="start",
            cwd=str(project),
            approval="manual",
        )
    )
    cold = registry.get(cold_reservation.participant.id)
    assert cold.transcript_domain is not None
    domain = Path(cold.transcript_domain)
    transcript = domain / "session_20260817_120000_sessabc1" / "messages.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text('{"role":"assistant","content":"cold"}\n', encoding="utf-8")
    cold.session_id = "sessabc1-1111-2222-3333"
    cold.session_correlation = "exact"
    cold.transcript_location = str(transcript)
    registry.store.upsert_participant(cold)
    registry.mark_dead(cold.id)

    live_reservation = await spawner.reserve(
        SpawnRequest(
            harness="vibe",
            prompt="resume once",
            cwd=str(project),
            approval="manual",
            resume="sessabc1-1111-2222-3333",
        )
    )
    live = live_reservation.participant

    with pytest.raises(BadRequest, match=f"trusted owner {live.id} is still live"):
        await spawner.reserve(
            SpawnRequest(
                harness="vibe",
                prompt="resume twice",
                cwd=str(project),
                approval="manual",
                resume="sessabc1-1111-2222-3333",
            )
        )


async def test_vibe_resume_refuses_unrelated_trusted_row_for_marked_domain(
    registry, tmp_path, terminal_provider
):
    project = tmp_path / "project"
    project.mkdir()
    domain = tmp_path / "isolated-vibe"
    domain.mkdir()
    owner = registry.register(
        harness="vibe",
        pane=None,
        cwd=str(project),
        session_id="owner-session-1111",
    )
    (domain / ISOLATION_MARKER).write_text(
        isolation_marker_text(participant_id=owner.id, transcript_domain=domain),
        encoding="utf-8",
    )
    owner.session_correlation = "exact"
    owner.transcript_domain = str(domain.resolve())
    registry.store.upsert_participant(owner)
    unrelated = registry.register(
        harness="vibe",
        pane=None,
        cwd=str(project),
        session_id="resume-session-2222",
    )
    unrelated.session_correlation = "exact"
    unrelated.transcript_domain = str(domain.resolve())
    registry.store.upsert_participant(unrelated)
    registry.mark_dead(unrelated.id)

    with pytest.raises(BadRequest, match="different Theater session lineage"):
        await Spawner(registry).reserve(
            SpawnRequest(
                harness="vibe",
                prompt="continue",
                cwd=str(project),
                approval="manual",
                resume="resume-session-2222",
            )
        )

    assert terminal_provider.creations == []


async def test_vibe_resume_refuses_legacy_shared_root(registry, tmp_path, terminal_provider):
    project = tmp_path / "project"
    project.mkdir()
    shared = tmp_path / "shared-vibe"
    shared.mkdir()
    predecessor = registry.register(
        harness="vibe",
        pane=None,
        cwd=str(project),
        session_id="sessabc1-1111-2222-3333",
    )
    predecessor.session_correlation = "proven"
    predecessor.transcript_domain = str(shared.resolve())
    registry.store.upsert_participant(predecessor)
    registry.mark_dead(predecessor.id)

    with pytest.raises(BadRequest, match="Rebind or migrate"):
        await Spawner(registry).reserve(
            SpawnRequest(
                harness="vibe",
                prompt="continue",
                cwd=str(project),
                approval="manual",
                resume="sessabc1-1111-2222-3333",
            )
        )

    assert terminal_provider.creations == []


async def test_resume_persists_floor_on_successor(registry, resume_harness, monkeypatch, tmp_path):
    """A resume spawn captures the predecessor's stream floor on the successor."""
    from theater.resume_floor import UNKNOWN_FLOOR

    monkeypatch.setattr("theater.daemon.spawning.service.shutil.which", lambda b: f"/usr/bin/{b}")
    transcript_path = tmp_path / "messages.jsonl"
    transcript_path.write_text('{"role":"assistant","content":"old"}\n', encoding="utf-8")
    predecessor = trusted_resume(registry, harness="resume-spawn-test")
    predecessor = registry.get(predecessor.id)
    predecessor.transcript_location = str(transcript_path)
    registry.store.upsert_participant(predecessor)
    spawner = Spawner(registry)
    req = SpawnRequest(
        harness="resume-spawn-test",
        prompt="",
        cwd="/tmp",
        approval="edits",
        resume="sess-abc",
    )
    spawned = (await spawner.reserve(req)).participant
    reloaded = registry.store.get_participant(spawned.id)
    assert reloaded.resume_floor is not None
    assert reloaded.resume_floor != UNKNOWN_FLOOR
    assert reloaded.source_checkpoint is None


async def test_resume_floor_unknown_when_transcript_missing(
    registry, resume_harness, monkeypatch, tmp_path
):
    """A missing predecessor transcript location produces an unknown floor."""
    from theater.resume_floor import UNKNOWN_FLOOR

    monkeypatch.setattr("theater.daemon.spawning.service.shutil.which", lambda b: f"/usr/bin/{b}")
    predecessor = trusted_resume(registry, harness="resume-spawn-test")
    predecessor = registry.get(predecessor.id)
    # transcript_location stays None — the predecessor never attached
    registry.store.upsert_participant(predecessor)
    spawner = Spawner(registry)
    req = SpawnRequest(
        harness="resume-spawn-test",
        prompt="",
        cwd="/tmp",
        approval="edits",
        resume="sess-abc",
    )
    spawned = (await spawner.reserve(req)).participant
    reloaded = registry.store.get_participant(spawned.id)
    assert reloaded.resume_floor == UNKNOWN_FLOOR
    assert reloaded.source_checkpoint is None


async def test_cold_spawn_has_no_floor(registry, resume_harness, monkeypatch):
    """A cold spawn (no resume) has a NULL resume_floor."""
    monkeypatch.setattr("theater.daemon.spawning.service.shutil.which", lambda b: f"/usr/bin/{b}")
    spawner = Spawner(registry)
    req = SpawnRequest(
        harness="resume-spawn-test",
        prompt="do thing",
        cwd="/tmp",
        approval="edits",
    )
    spawned = (await spawner.reserve(req)).participant
    reloaded = registry.store.get_participant(spawned.id)
    assert reloaded.resume_floor is None
    assert reloaded.source_checkpoint is None


async def test_resume_floor_unknown_when_file_unreadable(
    registry, resume_harness, monkeypatch, tmp_path
):
    """An unreadable predecessor transcript produces an unknown floor."""
    from theater.resume_floor import UNKNOWN_FLOOR

    monkeypatch.setattr("theater.daemon.spawning.service.shutil.which", lambda b: f"/usr/bin/{b}")
    transcript_path = tmp_path / "missing" / "messages.jsonl"
    predecessor = trusted_resume(registry, harness="resume-spawn-test")
    predecessor = registry.get(predecessor.id)
    predecessor.transcript_location = str(transcript_path)
    registry.store.upsert_participant(predecessor)
    spawner = Spawner(registry)
    req = SpawnRequest(
        harness="resume-spawn-test",
        prompt="",
        cwd="/tmp",
        approval="edits",
        resume="sess-abc",
    )
    spawned = (await spawner.reserve(req)).participant
    reloaded = registry.store.get_participant(spawned.id)
    assert reloaded.resume_floor == UNKNOWN_FLOOR
    assert reloaded.source_checkpoint is None


async def test_resume_by_participant_id_resolves_to_session_id(
    registry, resume_harness, monkeypatch
):
    """A Theater participant id in resume is resolved to the harness session id."""
    monkeypatch.setattr("theater.daemon.spawning.service.shutil.which", lambda b: f"/usr/bin/{b}")
    p = trusted_resume(registry, harness="resume-spawn-test", session_id="native-sess-123")
    spawner = Spawner(registry)
    req = SpawnRequest(
        harness="resume-spawn-test",
        prompt="",
        cwd="/tmp",
        approval="edits",
        resume=p.id,
    )
    await spawner.reserve(req)
    assert resume_harness.seen_resume == "native-sess-123"


async def test_resume_by_participant_id_wrong_harness_refused(
    registry, resume_harness, monkeypatch
):
    monkeypatch.setattr("theater.daemon.spawning.service.shutil.which", lambda b: f"/usr/bin/{b}")
    p = trusted_resume(registry, harness="resume-spawn-test", session_id="native-sess-123")
    spawner = Spawner(registry)
    req = SpawnRequest(
        harness="vibe",
        prompt="",
        cwd="/tmp",
        approval="edits",
        resume=p.id,
    )
    with pytest.raises(BadRequest, match="belongs to harness"):
        await spawner.reserve(req)


async def test_resume_by_participant_id_live_refused(registry, resume_harness, monkeypatch):
    monkeypatch.setattr("theater.daemon.spawning.service.shutil.which", lambda b: f"/usr/bin/{b}")
    p = trusted_resume(
        registry, harness="resume-spawn-test", session_id="native-sess-123", live=True
    )
    spawner = Spawner(registry)
    req = SpawnRequest(
        harness="resume-spawn-test",
        prompt="",
        cwd="/tmp",
        approval="edits",
        resume=p.id,
    )
    with pytest.raises(BadRequest, match="still live"):
        await spawner.reserve(req)


async def test_resume_by_participant_id_no_session_id_refused(
    registry, resume_harness, monkeypatch
):
    monkeypatch.setattr("theater.daemon.spawning.service.shutil.which", lambda b: f"/usr/bin/{b}")
    p = trusted_resume(registry, harness="resume-spawn-test", session_id="native-sess-123")
    p = registry.get(p.id)
    p.session_id = None
    registry.store.upsert_participant(p)
    spawner = Spawner(registry)
    req = SpawnRequest(
        harness="resume-spawn-test",
        prompt="",
        cwd="/tmp",
        approval="edits",
        resume=p.id,
    )
    with pytest.raises(BadRequest, match="has not recorded its harness session id"):
        await spawner.reserve(req)


async def test_resume_by_unknown_id_falls_through_to_native_path(
    registry, resume_harness, monkeypatch
):
    """A value that is not a participant id is treated as a native session id."""
    monkeypatch.setattr("theater.daemon.spawning.service.shutil.which", lambda b: f"/usr/bin/{b}")
    trusted_resume(registry, harness="resume-spawn-test", session_id="native-sess-456")
    spawner = Spawner(registry)
    req = SpawnRequest(
        harness="resume-spawn-test",
        prompt="",
        cwd="/tmp",
        approval="edits",
        resume="not-a-participant-id",
    )
    with pytest.raises(BadRequest, match="no trusted"):
        await spawner.reserve(req)
