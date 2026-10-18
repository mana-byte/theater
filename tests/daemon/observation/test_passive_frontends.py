"""Passive frontend registrations never replace durable completion, identity or attribution."""

from __future__ import annotations

import pytest

from tests.support.hybrid_source import ScriptedSource, outcome
from tests.support.pi_bridge import (
    GENERATION,
    PARTICIPANT,
    SESSION_A,
    SESSION_B,
    ScriptedPiPeer,
    bridge_snapshot,
)
from theater.daemon.jobs import JobManager
from theater.daemon.observation.live import LiveRegistration
from theater.daemon.observation.reducer import QuietClock
from theater.daemon.observation.service import Observer
from theater.daemon.observation.turns import TurnAccumulator
from theater.harness.builtin.plugins.opencode.live import OpenCodeTuiLiveSource
from theater.harness.builtin.plugins.pi.runtime import PiFrontendRuntime
from theater.harness.channels.composite import CompositeSource, EnrichmentBinding
from theater.harness.channels.hybrid import HybridSource
from theater.harness.contracts.channels import ChannelDeclaration, ChannelKind
from theater.harness.contracts.events import Event, EventKind, EventPath
from theater.harness.contracts.runtime import LiveChannelDeclaration, RuntimeNotification
from theater.harness.contracts.source import Batch, Source
from theater.models import Status

PASSIVE = LiveChannelDeclaration(
    channel=ChannelDeclaration(id="pi-frontend-live", kind=ChannelKind.LIVE),
    drives_job_completion=False,
)


async def test_passive_frontend_never_completes_a_legacy_delivered_turn():
    durable = ScriptedSource(Batch(status=Status.WORKING), Batch(status=Status.WORKING))
    live = ScriptedSource(Batch(status=Status.IDLE, terminal_evidence=(outcome(),)), Batch())
    source = HybridSource(durable=durable, live=live, live_channel=PASSIVE)
    first = await source.read()
    assert first.status is Status.IDLE
    assert first.terminal_evidence == ()
    # An old idle observation is not retained after the live source stops
    # confirming it; the ordinary transcript remains the fallback.
    assert (await source.read()).status is Status.WORKING


async def test_passive_pi_identity_is_rechecked_after_sibling_enrichment_awaits():
    trusted = [SESSION_A]
    peer = ScriptedPiPeer(responses={"pi.snapshot": bridge_snapshot()})
    runtime = PiFrontendRuntime(
        participant_id=PARTICIPANT,
        backend_generation=GENERATION,
        peer=peer,
        trusted_session_id_provider=lambda: trusted[0],
    )
    await runtime.attach()

    class SessionSwitch(Source):
        async def read(self):
            trusted[0] = SESSION_B
            return Batch()

    source = CompositeSource(
        primary=HybridSource(
            durable=ScriptedSource(Batch(status=Status.WORKING)),
            live=runtime.live_source(),
            live_channel=PASSIVE,
        ),
        enrichments=(
            EnrichmentBinding(
                SessionSwitch(),
                ChannelDeclaration(id="late-hook", kind=ChannelKind.HOOK),
            ),
        ),
    )
    try:
        assert (await source.read()).status is Status.WORKING
        assert (await runtime.live_source().read()).status is None
    finally:
        await runtime.aclose()


@pytest.mark.parametrize("change", ["trusted", "visible", "home", "disconnect", "expire", "busy"])
async def test_opencode_status_is_revalidated_after_sibling_await(change, monkeypatch):
    trusted = ["ses-a"]
    clock = [100.0]
    monkeypatch.setattr("theater.harness.builtin.plugins.opencode.live.monotonic", lambda: clock[0])
    live = OpenCodeTuiLiveSource(lambda: trusted[0])

    def publish(session, epoch, status):
        live.feed(
            RuntimeNotification(
                method="snapshot",
                params={
                    "session_id": session,
                    "route_session_id": session,
                    "session_epoch": epoch,
                    "status": {"type": status},
                },
            )
        )

    publish("ses-a", 1, "idle")

    class LaterSource(Source):
        async def read(self):
            if change == "trusted":
                trusted[0] = "ses-b"
            elif change == "visible":
                publish("ses-b", 2, "idle")
            elif change == "home":
                publish(None, 2, "idle")
            elif change == "disconnect":
                live.disconnected()
            elif change == "expire":
                clock[0] += 4
            else:
                publish("ses-a", 1, "busy")
            return Batch()

    source = CompositeSource(
        primary=HybridSource(
            durable=ScriptedSource(
                Batch(status=Status.IDLE if change == "busy" else Status.WORKING)
            ),
            live=live,
            live_channel=LiveChannelDeclaration(
                channel=ChannelDeclaration(id="opencode-tui-live", kind=ChannelKind.LIVE),
                drives_job_completion=False,
            ),
        ),
        enrichments=(
            EnrichmentBinding(LaterSource(), ChannelDeclaration(id="late", kind=ChannelKind.HOOK)),
        ),
    )
    batch = await source.read()
    assert batch.status is Status.WORKING
    assert batch.terminal_evidence == ()


class EmptySource(Source):
    async def read(self) -> Batch:
        return Batch()


class RecordingJobs(JobManager):
    """A JobManager that records every observe_paths call instead of accumulating."""

    def __init__(self, store) -> None:
        super().__init__(store)
        self.touches: list[tuple[str, list[str]]] = []

    def observe_paths(self, handle, paths) -> None:
        self.touches.append((handle, [p.path for p in paths]))


def _wired(registry, *, cwd: str) -> tuple[Observer, RecordingJobs, str, EmptySource]:
    """An observer whose jobs record path touches, with one running spawn job."""
    jobs = RecordingJobs(registry.store)
    participant = registry.register(harness="pi", pane=None, cwd=cwd)
    jobs.create(
        handle="job-1",
        caller_id="caller",
        target_id=participant.id,
        kind="spawn",
        prompt="inspect README",
        cwd=cwd,
    )
    observer = Observer(registry, harnesses={}, jobs=jobs)
    return observer, jobs, participant.id, EmptySource()


def _paths_batch() -> Batch:
    return Batch(
        events=(
            Event(
                kind=EventKind.TOOL_CALL,
                tool_name="read",
                turn_id="turn-1",
                paths=(EventPath(path="README.md", mode="read"),),
            ),
        ),
        progressed=True,
    )


def test_passive_registration_attributes_touches_via_oldest_running_job(registry, tmp_path):
    """A passive registration falls back to the legacy heuristic, not to nothing."""
    observer, jobs, pid, source = _wired(registry, cwd=str(tmp_path))
    registration = LiveRegistration(
        participant_id=pid,
        live_source=source,
        channel=PASSIVE,
        backend_generation=1,
        native_session_id=None,
        evidence_sink=None,
        active_job_for_turn=None,
    )
    observer._apply_source_batch(
        pid, source, _paths_batch(), QuietClock(), TurnAccumulator(), registration=registration
    )
    assert jobs.touches == [("job-1", ["README.md"])]


def test_registration_with_working_mapper_keeps_exact_attribution(registry, tmp_path):
    """A mapper-backed registration attributes by the mapped job, not the heuristic."""
    observer, jobs, pid, source = _wired(registry, cwd=str(tmp_path))
    # A second, newer running job: the heuristic would pick job-1 (oldest).
    jobs.create(
        handle="job-2",
        caller_id="caller",
        target_id=pid,
        kind="spawn",
        prompt="second turn",
        cwd=str(tmp_path),
    )
    mapped = jobs.get("job-2")

    def mapper(participant_id, *, backend_generation, native_session_id, native_turn_id):
        assert participant_id == pid
        assert backend_generation == 1
        assert native_session_id == "ses-1"
        assert native_turn_id == "turn-1"
        return mapped

    registration = LiveRegistration(
        participant_id=pid,
        live_source=source,
        channel=PASSIVE,
        backend_generation=1,
        native_session_id="ses-1",
        evidence_sink=None,
        active_job_for_turn=mapper,
    )
    observer._apply_source_batch(
        pid, source, _paths_batch(), QuietClock(), TurnAccumulator(), registration=registration
    )
    assert jobs.touches == [("job-2", ["README.md"])]


def test_registration_with_none_mapping_mapper_records_no_touch(registry, tmp_path):
    """A mapper that fails closed to None attributes nothing — and must not crash."""
    observer, jobs, pid, source = _wired(registry, cwd=str(tmp_path))

    def mapper(participant_id, *, backend_generation, native_session_id, native_turn_id):
        return None

    registration = LiveRegistration(
        participant_id=pid,
        live_source=source,
        channel=PASSIVE,
        backend_generation=1,
        native_session_id="ses-1",
        evidence_sink=None,
        active_job_for_turn=mapper,
    )
    result = observer._apply_source_batch(
        pid, source, _paths_batch(), QuietClock(), TurnAccumulator(), registration=registration
    )
    assert result is True
    assert jobs.touches == []
