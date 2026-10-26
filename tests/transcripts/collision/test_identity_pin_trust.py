"""Trusted pins resist rotation, strangers and symlinks; rejected candidates are discarded."""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from tests.support.collision_rig import collision_observer as shared_collision_observer
from tests.support.collision_rig import collision_registry as shared_collision_registry
from tests.support.collision_rig import make_session
from tests.support.collision_rig import vibe_tree as shared_vibe_tree
from tests.support.observation import apply_batch, run_quiet_step
from tests.support.shipped import VibeHarness, VibeObserver
from theater.daemon import methods as methods_mod
from theater.daemon.jobs import JobManager
from theater.daemon.observer import Observer, QuietClock, TurnAccumulator
from theater.daemon.rpc import transcripts as transcripts_mod
from theater.harness.builtin.plugins.vibe.constants import ISOLATION_MARKER
from theater.harness.builtin.plugins.vibe.isolation import isolation_marker_text
from theater.harness.source import Attachment, Batch, Source
from theater.models import TranscriptIdentityLost
from theater.provenance import TranscriptProvenance

vibe_tree = shared_vibe_tree  # the shared fixture
collision_registry = shared_collision_registry  # the shared fixture
collision_observer = shared_collision_observer  # the shared fixture


class _StagedSource(Source):
    def __init__(self):
        self.committed = False
        self.discarded = False

    async def read(self) -> Batch:
        return Batch()

    def commit_attachment(self) -> None:
        self.committed = True

    def discard_attachment(self) -> None:
        self.discarded = True


async def test_read_transcript_pinned_symlink_escape_fails_closed(
    collision_registry, tmp_path, monkeypatch
):
    project = tmp_path / "project"
    project.mkdir()
    domain = tmp_path / "isolated-vibe"
    domain.mkdir()
    outside = tmp_path / "outside.jsonl"
    outside.write_text(json.dumps({"role": "assistant", "content": "outside"}) + "\n")
    link = domain / "escape.jsonl"
    link.symlink_to(outside)
    participant = collision_registry.register(
        harness="vibe",
        pane=None,
        cwd=str(project),
        session_id="sessabc1-1111-2222-3333",
    )
    (domain / ISOLATION_MARKER).write_text(
        isolation_marker_text(participant_id=participant.id, transcript_domain=domain),
        encoding="utf-8",
    )
    participant.session_correlation = "operator"
    participant.transcript_domain = str(domain.resolve())
    participant.transcript_location = str(link)
    collision_registry.store.upsert_participant(participant)
    harness = VibeHarness(root=tmp_path / "shared-vibe")
    monkeypatch.setitem(transcripts_mod.HARNESSES, "vibe", harness)
    daemon = SimpleNamespace(
        registry=collision_registry,
        observer=Observer(collision_registry, {"vibe": harness}),
    )

    with pytest.raises(TranscriptIdentityLost, match="no longer exists"):
        await methods_mod.METHODS["read_transcript"](daemon, {"id": participant.id})


def test_trusted_dead_owner_blocks_stranger_but_allows_successor(collision_registry, vibe_tree):
    observer = Observer(collision_registry, harnesses={})
    dead = collision_registry.register(
        harness="vibe",
        pane=None,
        cwd=str(vibe_tree["project"]),
        session_id="aa5d2d32-1111-2222-3333",
    )
    dead.session_correlation = "exact"
    dead.transcript_location = str(vibe_tree["transcript_b"])
    collision_registry.store.upsert_participant(dead)
    collision_registry.mark_dead(dead.id)

    stranger = collision_registry.register(
        harness="vibe",
        pane=None,
        cwd=str(vibe_tree["project"]),
        session_id="other-session",
    )
    stranger_source = _StagedSource()
    stranger_batch = Batch(
        attached=Attachment(
            location=str(vibe_tree["transcript_b"]),
            session_id="other-session",
            correlation="exact",
        )
    )
    assert not observer._accept_attachment(stranger.id, stranger_source, stranger_batch)
    assert stranger_source.discarded is True

    successor = collision_registry.register(
        harness="vibe",
        pane=None,
        cwd=str(vibe_tree["project"]),
        session_id="aa5d2d32-1111-2222-3333",
    )
    successor_source = _StagedSource()
    successor_batch = Batch(
        attached=Attachment(
            location=str(vibe_tree["transcript_b"]),
            session_id="aa5d2d32-1111-2222-3333",
            correlation="exact",
        )
    )
    assert observer._accept_attachment(successor.id, successor_source, successor_batch)
    assert successor_source.committed is True


def test_proven_attach_does_not_downgrade_existing_exact_session_id(collision_registry, vibe_tree):
    observer = Observer(collision_registry, harnesses={})
    participant = collision_registry.register(
        harness="vibe",
        pane=None,
        cwd=str(vibe_tree["project"]),
        session_id="exact-session",
    )
    participant.session_correlation = "exact"
    collision_registry.store.upsert_participant(participant)

    observer._on_attach(
        participant.id,
        Attachment(
            location=str(vibe_tree["transcript_b"]),
            session_id="process-proven-session",
            correlation="proven",
        ),
    )

    after = collision_registry.get(participant.id)
    assert after.session_id == "exact-session"
    assert after.session_correlation == "exact"
    assert after.transcript_location == str(vibe_tree["transcript_b"])


async def test_untrusted_global_vibe_rotation_is_quarantined_even_with_distinct_domain(
    collision_registry, vibe_tree, tmp_path
):
    """A distinct domain avoids false sibling collisions, but does not prove ownership."""
    adapter = VibeObserver(root=vibe_tree["root"])
    observer = Observer(collision_registry, harnesses={})
    global_domain = str(vibe_tree["root"].resolve())

    incumbent = collision_registry.register(
        harness="vibe",
        pane=None,
        cwd=str(vibe_tree["project"]),
        session_id="a00bff57-1111-2222-3333",
    )
    incumbent.transcript_domain = global_domain
    collision_registry.store.upsert_participant(incumbent)
    source = adapter.open_source(
        cwd=incumbent.cwd,
        session_id=incumbent.session_id,
        session_provenance=TranscriptProvenance.EXACT,
    )
    observer._sources[incumbent.id] = source
    initial = await source.read()
    assert observer._accept_attachment(incumbent.id, source, initial)

    isolated = collision_registry.register(harness="vibe", pane=None, cwd=str(vibe_tree["project"]))
    isolated.transcript_domain = str((tmp_path / "private-vibe").resolve())
    collision_registry.store.upsert_participant(isolated)

    rotated = make_session(vibe_tree["root"], "zzzzzzzz", str(vibe_tree["project"]))
    candidate = await source.refresh()
    evidence = await source.probe_identity_loss()
    assert candidate.attached is None
    assert evidence is not None
    assert evidence.location == str(rotated)
    assert source.path != rotated


async def test_rejected_rotation_keeps_events_and_awaits_session_local(
    collision_registry, vibe_tree
):
    """A sibling's newer transcript must never replace an accepted cursor.

    This is the complete regression for the two reported symptoms. Both Vibe
    participants know their exact sessions initially. A's cwd-only refresh
    then finds B's newer file and is refused. B's next turn must not flap A's
    status or appear on A's bus; A's own old file must still be readable, and
    its turn end must wake an await already blocked on A's job.
    """
    observer_adapter = VibeObserver(root=vibe_tree["root"])
    p_a = collision_registry.register(
        harness="vibe",
        pane=None,
        cwd=str(vibe_tree["project"]),
        session_id="a00bff57-1111-2222-3333",
    )
    p_b = collision_registry.register(
        harness="vibe",
        pane=None,
        cwd=str(vibe_tree["project"]),
        session_id="aa5d2d32-1111-2222-3333",
    )
    jobs = JobManager(collision_registry.store)
    observer = Observer(collision_registry, harnesses={}, jobs=jobs)
    source_a = observer_adapter.open_source(
        cwd=p_a.cwd,
        session_id=p_a.session_id,
        after=None,
        session_provenance=TranscriptProvenance.EXACT,
    )
    source_b = observer_adapter.open_source(
        cwd=p_b.cwd,
        session_id=p_b.session_id,
        after=None,
        session_provenance=TranscriptProvenance.EXACT,
    )
    turns_a = TurnAccumulator()

    batch_a = await source_a.read()
    batch_b = await source_b.read()
    assert observer._accept_attachment(p_a.id, source_a, batch_a)
    assert observer._accept_attachment(p_b.id, source_b, batch_b)
    apply_batch(observer, p_a.id, batch_a, QuietClock(), turns_a, source=source_a)
    apply_batch(observer, p_b.id, batch_b, QuietClock(), TurnAccumulator(), source=source_b)
    assert len(observer._bound_transcripts) == 2

    # Cwd-only relocation finds B only through the non-committable loss probe.
    # It must be a no-op on A's accepted path.
    candidate = await source_a.refresh()
    evidence = await source_a.probe_identity_loss()
    assert candidate.attached is None
    assert evidence is not None
    assert evidence.location == str(vibe_tree["transcript_b"])
    assert source_a.path == vibe_tree["transcript_a"]
    assert not any(key[0] == p_a.id for key in observer._source_errors)

    # Rejection throttles only the relocate arm. The same discoverable foreign
    # candidate must not be re-read on every 250ms poll forever.
    relocate_clock = QuietClock()
    relocate_clock.quiet_since = time.monotonic() - observer.relocate - 1
    await run_quiet_step(
        observer, p_a.id, observer_adapter, source_a, relocate_clock, TurnAccumulator()
    )
    assert relocate_clock.quiet_for(time.monotonic()) < 0.1

    before = collision_registry.store.bus_tail(limit=500)[-1]["id"]
    with vibe_tree["transcript_b"].open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"role": "user", "content": "foreign prompt"}) + "\n")
        fh.write(json.dumps({"role": "assistant", "content": "foreign answer"}) + "\n")

    # A remains on its own quiet transcript. Only B can publish B's append.
    assert not (await source_a.read()).events
    foreign = await source_b.read()
    apply_batch(observer, p_b.id, foreign, QuietClock(), TurnAccumulator(), source=source_b)
    rows = collision_registry.store.bus_tail(limit=500, after_id=before)
    agent_rows = [r for r in rows if r["kind"].startswith("agent.")]
    assert agent_rows
    assert {r["from_id"] for r in agent_rows} == {p_b.id}
    assert collision_registry.get(p_a.id).status.value == "idle"

    jobs.create(handle="await-a", caller_id="caller", target_id=p_a.id, kind="send")
    waiting = asyncio.create_task(jobs.await_jobs(["await-a"], max_wait=1.0))
    await asyncio.sleep(0)
    assert not waiting.done(), "the regression must exercise an already-blocked await"
    with vibe_tree["transcript_a"].open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"role": "assistant", "content": "A is done"}) + "\n")

    own = await source_a.read()
    apply_batch(observer, p_a.id, own, QuietClock(), turns_a, source=source_a)
    result = await asyncio.wait_for(waiting, timeout=5)
    assert result[0].state == "done"
    assert result[0].result == "A is done"


async def test_unique_heuristic_candidate_is_never_auto_adopted(collision_registry, vibe_tree):
    harness = VibeHarness(root=vibe_tree["root"])
    observer = Observer(collision_registry, {"vibe": harness})
    participant = collision_registry.register(
        harness="vibe", pane=None, cwd=str(vibe_tree["project"])
    )
    source = harness.observer.open_source(cwd=participant.cwd)

    batch = await source.read()

    assert batch.attached is not None
    assert batch.attached.correlation == "heuristic"
    assert not observer._accept_attachment(participant.id, source, batch)
    assert source.path is None
    assert collision_registry.get(participant.id).transcript_location is None


async def test_accepted_rotation_commits_and_releases_the_old_binding(
    collision_registry, vibe_tree
):
    """A genuinely unowned rotation atomically replaces the old binding."""
    adapter = VibeObserver(root=vibe_tree["root"], isolated=True)
    p = collision_registry.register(
        harness="vibe",
        pane=None,
        cwd=str(vibe_tree["project"]),
        session_id="a00bff57-1111-2222-3333",
    )
    p.session_correlation = "exact"
    collision_registry.store.upsert_participant(p)
    observer = Observer(collision_registry, harnesses={})
    source = adapter.open_source(
        cwd=p.cwd,
        session_id=p.session_id,
        after=None,
        session_provenance=TranscriptProvenance.EXACT,
    )
    initial = await source.read()
    assert observer._accept_attachment(p.id, source, initial)
    old = str(vibe_tree["transcript_a"])
    assert observer._bound_transcripts[old] == p.id

    newest = make_session(vibe_tree["root"], "zzzzzzzz", str(vibe_tree["project"]))
    candidate = await source.refresh()
    assert candidate.attached is not None
    assert source.path == vibe_tree["transcript_a"]
    assert observer._accept_attachment(p.id, source, candidate)

    assert source.path == newest
    assert old not in observer._bound_transcripts
    assert observer._bound_transcripts[str(newest)] == p.id


class IncompleteAttachmentSource(Source):
    async def read(self) -> Batch:
        return Batch(attached=Attachment("somewhere", correlation="exact"))


async def test_attachment_source_without_handshake_fails_loudly(collision_registry):
    """A third-party source cannot silently reintroduce eager attachment."""
    p = collision_registry.register(harness="vibe", pane=None, cwd="/tmp")
    observer = Observer(collision_registry, harnesses={})
    source = IncompleteAttachmentSource()
    batch = await source.read()

    with pytest.raises(NotImplementedError, match="commit_attachment"):
        observer._accept_attachment(p.id, source, batch)


async def test_attachment_check_failure_discards_the_candidate(
    collision_registry, vibe_tree, monkeypatch
):
    """A transient store failure must not leave every later read wedged."""
    adapter = VibeObserver(root=vibe_tree["root"])
    p = collision_registry.register(
        harness="vibe",
        pane=None,
        cwd=str(vibe_tree["project"]),
        session_id="a00bff57-1111-2222-3333",
    )
    p.session_correlation = "exact"
    collision_registry.store.upsert_participant(p)
    observer = Observer(collision_registry, harnesses={})
    source = adapter.open_source(
        cwd=p.cwd,
        session_id=p.session_id,
        after=None,
        session_provenance=TranscriptProvenance.EXACT,
    )
    batch = await source.read()
    assert batch.attached is not None
    observer._bound_transcripts[batch.attached.location] = "other"

    original = collision_registry.store.get_participant

    def fail_once(_pid):
        raise OSError("transient store failure")

    monkeypatch.setattr(collision_registry.store, "get_participant", fail_once)
    with pytest.raises(OSError, match="transient"):
        observer._accept_attachment(p.id, source, batch)
    monkeypatch.setattr(collision_registry.store, "get_participant", original)

    retry = await source.read()
    assert retry.attached is not None
    source.discard_attachment()


async def test_observer_releases_binding_on_watcher_end(
    collision_registry, vibe_tree, collision_observer
):
    """When a watcher ends, its transcript binding is released.

    This ensures a new participant starting later in the same cwd can bind
    the path the previous one held.
    """
    from tests.support.waiting import until

    p_a = collision_registry.register(
        harness="vibe",
        pane=None,
        cwd=str(vibe_tree["project"]),
        session_id="aa5d2d32-1111-2222-3333",
    )
    p_a.session_correlation = "exact"
    collision_registry.store.upsert_participant(p_a)

    assert await until(lambda: len(collision_observer._bound_transcripts) > 0, timeout=3.0)

    # Kill the participant — the watcher ends and releases the binding.
    collision_registry.mark_dead(p_a.id)

    assert await until(lambda: len(collision_observer._bound_transcripts) == 0, timeout=3.0)
