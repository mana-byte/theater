"""The first-class live/durable composition for native runtime wiring.

``HybridSource`` is not a ``CompositeSource`` enrichment: the live channel
has its own authority regime. These tests pin that regime — durable keeps
attachment, identity, history, and the persisted cursor; live owns the
current turn, healthy status, and exact terminal evidence — plus the
evidence replay and fact-merge semantics that make delayed durable data
enrich history without reopening terminal turns.
"""

from __future__ import annotations

import asyncio

import pytest

from theater.harness.channels.hybrid import HybridSource, HybridSourceError
from theater.harness.channels.wakeup import WakeupSignal
from theater.harness.contracts.channels import (
    ChannelDeclaration,
    ChannelHealth,
    ChannelHealthState,
    ChannelKind,
)
from theater.harness.contracts.events import Event, EventKind
from theater.harness.contracts.runtime import (
    LiveChannelDeclaration,
    NativeTurnOutcome,
    NativeTurnTerminal,
    ResultCompleteness,
    ResultProvenance,
)
from theater.harness.contracts.source import Batch, Source
from theater.harness.contracts.trajectory import TrajectoryFact
from theater.models import Status
from theater.trajectory.enums import TrajectoryKind, TrajectoryStatus

LIVE = ChannelDeclaration(id="codex-live", kind=ChannelKind.LIVE)
TRANSCRIPT = ChannelDeclaration(id="transcript", kind=ChannelKind.TRANSCRIPT)
DATABASE = ChannelDeclaration(id="history-db", kind=ChannelKind.DATABASE)


def outcome(turn: str = "turn-1", *, session: str = "session-1") -> NativeTurnOutcome:
    return NativeTurnOutcome(
        native_session_id=session,
        native_turn_id=turn,
        terminal=NativeTurnTerminal.COMPLETED,
        result="the answer",
        completeness=ResultCompleteness.COMPLETE,
        provenance=ResultProvenance.NATIVE_EVIDENCE,
    )


def fact(
    native_id: str,
    *,
    status: TrajectoryStatus = TrajectoryStatus.RUNNING,
    revision: int = 1,
    summary: str = "",
) -> TrajectoryFact:
    return TrajectoryFact(
        kind=TrajectoryKind.ASSISTANT,
        summary=summary or f"{native_id} {status.value}",
        status=status,
        native_id=native_id,
        revision=revision,
    )


class ScriptedSource(Source):
    """A Source that drains scripted batches and records its lifecycle."""

    def __init__(
        self,
        *batches: Batch,
        live_delay: float = 0.0,
        fail_reads: bool = False,
    ) -> None:
        self.batches = list(batches)
        self.live_delay = live_delay
        self.fail_reads = fail_reads
        self.closed = False
        self.acked = False
        self.rolled_back = False
        self.checkpoint: str | None = None
        self.pending_checkpoint: str | None = None

    async def read(self) -> Batch:
        if self.fail_reads:
            raise RuntimeError("channel exploded")
        if self.live_delay:
            await asyncio.sleep(self.live_delay)
        return self.batches.pop(0) if self.batches else Batch()

    async def refresh(self) -> Batch:
        return self.batches.pop(0) if self.batches else Batch()

    def source_checkpoint(self) -> str | None:
        return self.checkpoint

    def pending_source_checkpoint(self) -> str | None:
        return self.pending_checkpoint

    def acknowledge_source_checkpoint(self) -> None:
        self.acked = True
        self.pending_checkpoint = None

    def rollback_source_checkpoint(self) -> None:
        self.rolled_back = True

    async def aclose(self) -> None:
        self.closed = True


def hybrid(
    durable: Source,
    live: Source,
    *,
    live_channel: LiveChannelDeclaration | None = None,
    durable_channel: ChannelDeclaration = TRANSCRIPT,
    **kwargs,
) -> HybridSource:
    return HybridSource(
        durable=durable,
        live=live,
        live_channel=live_channel or LiveChannelDeclaration(channel=LIVE),
        durable_channel=durable_channel,
        **kwargs,
    )


# ---- construction validation ---------------------------------------------


def test_live_channel_must_be_live_kind():
    with pytest.raises(ValueError, match="kind"):
        hybrid(ScriptedSource(), ScriptedSource(), live_channel=LiveChannelDeclaration(TRANSCRIPT))


def test_durable_channel_must_be_durable_kind():
    with pytest.raises(HybridSourceError, match="durable"):
        hybrid(
            ScriptedSource(),
            ScriptedSource(),
            durable_channel=ChannelDeclaration(id="liveish", kind=ChannelKind.LIVE),
        )


def test_channel_ids_must_be_distinct():
    shared = ChannelDeclaration(id="same", kind=ChannelKind.TRANSCRIPT)
    live = ChannelDeclaration(id="same", kind=ChannelKind.LIVE)
    with pytest.raises(HybridSourceError, match="declared twice"):
        hybrid(
            ScriptedSource(),
            ScriptedSource(),
            live_channel=LiveChannelDeclaration(live),
            durable_channel=shared,
        )


def test_live_read_timeout_must_be_positive_finite():
    for bad in (0, -1, float("inf")):
        with pytest.raises(HybridSourceError, match="live_read_timeout"):
            hybrid(ScriptedSource(), ScriptedSource(), live_read_timeout=bad)


# ---- status authority -------------------------------------------------------


async def test_live_status_wins_while_healthy():
    durable = ScriptedSource(Batch(status=Status.IDLE))
    live = ScriptedSource(Batch(status=Status.WORKING))
    source = hybrid(durable, live)

    batch = await source.read()

    assert batch.status is Status.WORKING


async def test_delayed_durable_status_cannot_regress_live_status():
    durable = ScriptedSource(Batch(), Batch(status=Status.IDLE))
    live = ScriptedSource(Batch(status=Status.WORKING), Batch())
    source = hybrid(durable, live)

    assert (await source.read()).status is Status.WORKING
    # The live channel spoke once and stays healthy; a durable record that
    # lags behind must not pull the status back.
    assert (await source.read()).status is Status.WORKING


async def test_degraded_live_returns_to_durable_inference():
    durable = ScriptedSource(Batch(), Batch(status=Status.IDLE))
    live = ScriptedSource(Batch(status=Status.WORKING))
    source = hybrid(durable, live)

    assert (await source.read()).status is Status.WORKING
    live.fail_reads = True
    # The live channel can no longer speak; durable inference resumes.
    assert (await source.read()).status is Status.IDLE


async def test_live_read_timeout_degrades_without_failing_durable():
    durable = ScriptedSource(Batch(progressed=True), Batch(progressed=True))
    live = ScriptedSource(live_delay=5.0)
    source = hybrid(durable, live, live_read_timeout=0.01)

    first = await source.read()

    assert first.progressed is True
    health = {h.channel_id: h for h in source.health_snapshot()}
    assert health["codex-live"].state.value in {"degraded", "failed"}


async def test_live_read_exception_degrades_and_durable_flows():
    durable = ScriptedSource(Batch(status=Status.WORKING))
    live = ScriptedSource()
    live.fail_reads = True
    source = hybrid(durable, live)

    batch = await source.read()

    assert batch.status is Status.WORKING


# ---- trajectory fact merge ----------------------------------------------------


async def test_fact_merge_prefers_terminal_on_revision_tie():
    durable = ScriptedSource(Batch(trajectory=(fact("i1", status=TrajectoryStatus.COMPLETED),)))
    live = ScriptedSource(Batch(trajectory=(fact("i1", status=TrajectoryStatus.RUNNING),)))
    source = hybrid(durable, live)

    batch = await source.read()

    assert [f.status for f in batch.trajectory] == [TrajectoryStatus.COMPLETED]


async def test_fact_merge_prefers_higher_revision():
    durable = ScriptedSource(Batch(trajectory=(fact("i1", revision=1),)))
    live = ScriptedSource(Batch(trajectory=(fact("i1", revision=2),)))
    source = hybrid(durable, live)

    batch = await source.read()

    assert [f.revision for f in batch.trajectory] == [2]


async def test_completed_native_item_stays_terminal():
    live = ScriptedSource(
        Batch(trajectory=(fact("i1", status=TrajectoryStatus.COMPLETED),)),
        Batch(trajectory=(fact("i1", status=TrajectoryStatus.RUNNING, revision=5),)),
    )
    source = hybrid(ScriptedSource(), live)

    first = await source.read()
    second = await source.read()

    assert [f.status for f in first.trajectory] == [TrajectoryStatus.COMPLETED]
    # A later replaceable delta can never reopen a completed native item.
    assert second.trajectory == ()


async def test_plain_facts_pass_through_unmerged():
    durable = ScriptedSource(Batch(trajectory=(fact(None),)))
    live = ScriptedSource(Batch(trajectory=(fact("i1"),)))
    source = hybrid(durable, live)

    batch = await source.read()

    assert [f.native_id for f in batch.trajectory] == ["i1", None]


# ---- terminal evidence staging and replay -------------------------------------


async def test_evidence_retained_until_delivered():
    live = ScriptedSource(Batch(terminal_evidence=(outcome(),)), Batch())
    source = hybrid(ScriptedSource(), live)

    first = await source.read()
    second = await source.read()

    assert len(first.terminal_evidence) == 1
    # Unacknowledged evidence is never overwritten by a later read: it
    # replays until the observer confirms durable routing.
    assert source.pending_terminal_evidence() is True
    assert len(second.terminal_evidence) == 1
    source.terminal_evidence_delivered()
    third = await source.read()
    assert third.terminal_evidence == ()
    assert source.pending_terminal_evidence() is False


async def test_acknowledge_clears_held_evidence():
    live = ScriptedSource(Batch(terminal_evidence=(outcome(),)), Batch())
    source = hybrid(ScriptedSource(), live)

    await source.read()
    source.acknowledge_source_checkpoint()
    replay = await source.read()

    assert replay.terminal_evidence == ()


async def test_rollback_retains_evidence_for_replay():
    live = ScriptedSource(
        Batch(terminal_evidence=(outcome(),)),
        Batch(),
        Batch(),
    )
    source = hybrid(ScriptedSource(), live)

    await source.read()
    source.rollback_source_checkpoint()
    replay = await source.read()
    still_held = await source.read()

    assert len(replay.terminal_evidence) == 1
    # A rollback rewinds the durable cursor but never exact terminal
    # evidence: it stays held and replays until delivered.
    assert len(still_held.terminal_evidence) == 1
    source.terminal_evidence_delivered()
    drained = await source.read()
    assert drained.terminal_evidence == ()


async def test_arm_terminal_evidence_replay_reroutes_after_sink_failure():
    live = ScriptedSource(Batch(terminal_evidence=(outcome(),)), Batch())
    source = hybrid(ScriptedSource(), live)

    await source.read()
    source.arm_terminal_evidence_replay()
    replay = await source.read()

    assert len(replay.terminal_evidence) == 1


async def test_checkpoint_quartet_delegates_to_durable_only():
    durable = ScriptedSource()
    durable.checkpoint = "durable-cursor"
    durable.pending_checkpoint = "durable-cursor"
    live = ScriptedSource()
    source = hybrid(durable, live)

    assert source.source_checkpoint() == "durable-cursor"
    assert source.pending_source_checkpoint() == "durable-cursor"
    source.acknowledge_source_checkpoint()
    assert durable.acked is True
    source.rollback_source_checkpoint()
    assert durable.rolled_back is True


class HealthReportingSource(ScriptedSource):
    """A Source whose own health snapshot can be scripted independently."""

    def __init__(self, *batches: Batch, state: ChannelHealthState) -> None:
        super().__init__(*batches)
        self.state = state

    def health_snapshot(self) -> tuple[ChannelHealth, ...]:
        return (ChannelHealth(channel_id="codex-live", state=self.state),)


def event(
    native_id: str | None,
    *,
    revision: int = 0,
    text: str = "",
) -> Event:
    return Event(kind=EventKind.ASSISTANT, text=text, native_id=native_id, revision=revision)


# ---- live health authority ----------------------------------------------------


async def test_unhealthy_live_health_relinquishes_remembered_status():
    for state in (ChannelHealthState.DEGRADED, ChannelHealthState.FAILED):
        durable = ScriptedSource(Batch(), Batch(status=Status.IDLE))
        live = HealthReportingSource(
            Batch(status=Status.WORKING), Batch(), state=ChannelHealthState.HEALTHY
        )
        source = hybrid(durable, live)

        assert (await source.read()).status is Status.WORKING
        live.state = state
        # A normal (even empty) batch from a source whose own health
        # snapshot reports DEGRADED/FAILED is a disconnect: the remembered
        # live status is no longer authoritative and durable inference wins.
        assert (await source.read()).status is Status.IDLE
        health = {h.channel_id: h for h in source.health_snapshot()}
        assert health["codex-live"].state is not ChannelHealthState.HEALTHY


async def test_healthy_live_keeps_remembered_status():
    durable = ScriptedSource(Batch(), Batch(status=Status.IDLE))
    live = HealthReportingSource(
        Batch(status=Status.WORKING), Batch(), state=ChannelHealthState.HEALTHY
    )
    source = hybrid(durable, live)

    assert (await source.read()).status is Status.WORKING
    assert (await source.read()).status is Status.WORKING


async def test_starting_live_health_is_neutral():
    durable = ScriptedSource(Batch(), Batch(status=Status.IDLE))
    live = HealthReportingSource(Batch(), Batch(), state=ChannelHealthState.STARTING)
    source = hybrid(durable, live)

    assert (await source.read()).status is None
    # STARTING never reported a status, so nothing masks durable inference.
    assert (await source.read()).status is Status.IDLE


async def test_unhealthy_live_evidence_still_flows():
    durable = ScriptedSource(Batch(status=Status.IDLE))
    live = HealthReportingSource(
        Batch(terminal_evidence=(outcome(),)), state=ChannelHealthState.DEGRADED
    )
    source = hybrid(durable, live)

    batch = await source.read()

    # A degraded live channel loses status authority, never exact evidence.
    assert len(batch.terminal_evidence) == 1
    assert batch.status is Status.IDLE


# ---- identified event dedupe ----------------------------------------------------


async def test_identified_events_dedupe_by_native_id():
    durable = ScriptedSource(Batch(events=(event("i1", text="durable"),)))
    live = ScriptedSource(Batch(events=(event("i1", text="live"),)))
    source = hybrid(durable, live)

    batch = await source.read()

    # Durable and live describe the same native item once; the earlier
    # (durable) event is kept on a revision tie.
    assert [(e.native_id, e.text) for e in batch.events] == [("i1", "durable")]


async def test_identified_event_higher_revision_wins():
    durable = ScriptedSource(Batch(events=(event("i1", revision=1, text="old"),)))
    live = ScriptedSource(Batch(events=(event("i1", revision=2, text="new"),)))
    source = hybrid(durable, live)

    batch = await source.read()

    assert [(e.native_id, e.revision, e.text) for e in batch.events] == [("i1", 2, "new")]


async def test_anonymous_events_pass_through_unmerged():
    durable = ScriptedSource(Batch(events=(event(None, text="d"),)))
    live = ScriptedSource(
        Batch(
            events=(
                event(None, text="l"),
                event("i1"),
            )
        )
    )
    source = hybrid(durable, live)

    batch = await source.read()

    # Anonymous events have no identity to reconcile: both pass through in
    # arrival order, alongside the one identified live event.
    assert [(e.native_id, e.text) for e in batch.events] == [
        (None, "d"),
        (None, "l"),
        ("i1", ""),
    ]


async def test_identified_event_for_prior_completed_item_is_dropped():
    live = ScriptedSource(
        Batch(trajectory=(fact("i1", status=TrajectoryStatus.COMPLETED),)),
        Batch(events=(event("i1", text="late replay"),)),
    )
    source = hybrid(ScriptedSource(), live)

    first = await source.read()
    second = await source.read()

    # The native item completed in a prior read; a later identified event
    # for it is a late replay and never repeats the completion.
    assert [f.native_id for f in first.trajectory] == ["i1"]
    assert second.events == ()


async def test_same_read_completion_event_passes():
    live = ScriptedSource(
        Batch(
            trajectory=(fact("i1", status=TrajectoryStatus.COMPLETED),),
            events=(event("i1", text="completing"),),
        )
    )
    source = hybrid(ScriptedSource(), live)

    batch = await source.read()

    # The completion ledger is snapshotted before this read's merge, so the
    # batch's own completion event is not dropped against its own fact.
    assert [e.text for e in batch.events] == ["completing"]


async def test_late_durable_replay_of_emitted_event_is_suppressed():
    live = ScriptedSource(Batch(events=(event("i1", text="live"),)))
    durable = ScriptedSource(Batch(), Batch(events=(event("i1", text="durable replay"),)))
    source = hybrid(durable, live)

    first = await source.read()
    second = await source.read()

    assert [e.text for e in first.events] == ["live"]
    # The durable transcript caught up after live already emitted the item:
    # the replay is suppressed, the native item is heard exactly once.
    assert second.events == ()


async def test_rollback_unemits_the_last_reads_events():
    live = ScriptedSource(Batch(events=(event("i1", text="live"),)))
    durable = ScriptedSource(Batch(), Batch(events=(event("i1", text="durable retry"),)))
    source = hybrid(durable, live)

    await source.read()
    source.rollback_source_checkpoint()
    replay = await source.read()

    # The rolled-back read's identity was un-emitted, so the re-read emits
    # the item instead of suppressing a batch that never applied.
    assert [e.text for e in replay.events] == ["durable retry"]


# ---- refresh, health, wakeup, close ---------------------------------------------


async def test_refresh_keeps_live_status_on_rotation_attach():
    durable = ScriptedSource(Batch(), Batch(attached=object()))
    live = ScriptedSource(Batch(status=Status.WORKING))
    source = hybrid(durable, live)
    await source.read()  # the live channel has spoken and is healthy

    refreshed = await source.refresh()

    assert refreshed.attached is not None
    assert refreshed.status is Status.WORKING


async def test_health_snapshot_covers_both_channels():
    source = hybrid(
        ScriptedSource(),
        ScriptedSource(),
        durable_channel=DATABASE,
        durable_channel_id="history-db",
    )

    health = {item.channel_id: item for item in source.health_snapshot()}

    assert set(health) == {"history-db", "codex-live"}


async def test_evidence_bearing_live_read_wakes_the_signal():
    signal = WakeupSignal()
    live = ScriptedSource(Batch(terminal_evidence=(outcome(),)))
    source = hybrid(ScriptedSource(), live, wakeup=signal)

    assert signal.is_set() is False
    await source.read()

    assert signal.is_set() is True


async def test_aclose_closes_both_halves():
    durable = ScriptedSource()
    live = ScriptedSource()
    source = hybrid(durable, live)

    await source.aclose()

    assert durable.closed and live.closed
