"""Vibe input status survives source polling and provider-screen fallback."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from shipped import VibeHarness

from tests.test_vibe_unified_store import SESSION_ID, make_state, public_message
from tests.test_vibe_unified_store import Store as VibeStore
from theater import paths
from theater.daemon.observer import Observer, QuietClock, TurnAccumulator
from theater.frontend import FrontendClient, StateSynchronizer
from theater.harness.builtin.plugins.vibe.observer import VibeObserver
from theater.harness.builtin.plugins.vibe.unified_source import UnifiedVibeSource
from theater.models import Status
from theater.provenance import TranscriptProvenance


async def test_question_dialog_overrides_quiet_running_store_in_full_watch(
    tmp_path, daemon, terminal_provider
):
    """Vibe 2.25.4 leaves the durable session running during its question callback."""
    storage = VibeStore(tmp_path)
    storage.publish(
        generation="0000000000000001",
        snapshot_sequence=0,
        state=make_state([], status="running"),
        watermark=1,
    )
    participant = daemon.registry.register(
        harness="vibe", pane=None, cwd="/tmp/work", session_id=SESSION_ID
    )
    participant.session_correlation = str(TranscriptProvenance.EXACT)
    participant.transcript_location = str(storage.current)
    participant.transcript_domain = str(tmp_path)
    daemon.store.upsert_participant(participant)
    terminal = terminal_provider.bind(daemon, participant.id)
    screens = Path(__file__).parent / "fixtures" / "screens"
    terminal_provider.screens[terminal] = (screens / "vibe_question.txt").read_text()
    observer = Observer(
        daemon.registry,
        {"vibe": VibeHarness(root=tmp_path, isolated=True)},
        poll=0.01,
        search=0.01,
        awaiting=0.01,
        sync=0.01,
    )
    observer.set_terminal_evidence_provider(daemon.presence)
    client = FrontendClient(paths.socket_path(), client_id="vibe-question-watch-test")
    follow = StateSynchronizer(client)
    try:
        await follow.refresh()
        observer.start()
        async with asyncio.timeout(2):
            while daemon.registry.get(participant.id).status is not Status.AWAITING_INPUT:
                await asyncio.sleep(0.01)
        assert participant.id in observer._sources
        for _ in range(5):
            await asyncio.sleep(0.02)
            projection = await follow.follow_once(wait_seconds=0)
            assert projection.participants[participant.id].status == "awaiting_input"
        terminal_provider.screens[terminal] = (screens / "vibe_idle.txt").read_text()
        async with asyncio.timeout(2):
            while daemon.registry.get(participant.id).status is not Status.IDLE:
                await asyncio.sleep(0.01)
        assert not terminal_provider.deliveries
        assert not terminal_provider.interruptions
        assert not terminal_provider.terminations
    finally:
        await observer.aclose()
        await client.close()


@pytest.mark.parametrize("restore_checkpoint", [False, True])
async def test_pending_approval_survives_polling_and_progress(
    tmp_path, registry, monkeypatch, restore_checkpoint
) -> None:
    storage = VibeStore(tmp_path)

    def publish(sequence, status, entries=()):
        state = make_state(list(entries), status=status)
        state["session"]["tokenUsage"] = {"inputTokens": 10, "outputTokens": 2}
        storage.publish(
            generation=f"{sequence + 1:016d}",
            snapshot_sequence=sequence,
            state=state,
            watermark=sequence + 1,
        )

    def open_source(checkpoint=None):
        return UnifiedVibeSource(
            VibeObserver(root=tmp_path, isolated=True),
            cwd="/tmp/work",
            session_id=SESSION_ID,
            after=None,
            session_provenance=TranscriptProvenance.EXACT,
            known_location=str(storage.current),
            source_checkpoint=checkpoint,
            count_initial=True,
        )

    async def capture(_participant_id):
        # An approval footer can be clipped while the spinner remains visible.
        return "Running tool… (Esc to interrupt)"

    publish(0, "blocked")
    participant = registry.register(harness="vibe", pane=None, cwd="/tmp/work")
    observer = Observer(registry, harnesses={}, awaiting=0, monotonic_clock=lambda: 10.0)
    monkeypatch.setattr(observer, "_capture", capture)
    source = open_source()
    try:
        assert observer._accept_attachment(participant.id, source, await source.read())
        source.acknowledge_source_checkpoint()
        if restore_checkpoint:
            checkpoint = source.source_checkpoint()
            await source.aclose()
            source = open_source(checkpoint)
            registry.set_status(participant.id, Status.WORKING)
            assert observer._accept_attachment(participant.id, source, await source.read())
            source.acknowledge_source_checkpoint()
        assert registry.get(participant.id).status is Status.AWAITING_INPUT

        # Initial usage, a quiet poll, and unrelated output all keep the pending approval.
        for phase in ("usage", "quiet", "output"):
            if phase == "output":
                publish(1, "blocked", [public_message("message-1", "assistant", "Still waiting")])
            batch = await source.read()
            assert batch.status is Status.AWAITING_INPUT
            assert batch.progressed is (phase != "quiet")
            observer._apply(participant.id, batch, QuietClock(), TurnAccumulator())
            observer._unblock_on_semantic_progress(participant.id, batch)
            await observer._screen_only(
                participant.id,
                VibeObserver(),
                QuietClock(screen_quiet_since=0.0),
                source_status=batch.status,
            )
            source.acknowledge_source_checkpoint()
            assert registry.get(participant.id).status is Status.AWAITING_INPUT

        publish(2, "running")
        resumed = await source.read()
        assert resumed.status is Status.WORKING
        observer._apply(participant.id, resumed, QuietClock(), TurnAccumulator())
        source.acknowledge_source_checkpoint()
        assert registry.get(participant.id).status is Status.WORKING
    finally:
        await source.aclose()
        await observer.aclose()
