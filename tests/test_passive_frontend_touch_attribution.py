"""Passive live registrations keep path-touch attribution alive.

Frontend-registered participants (Pi, OpenCode) register with
``native_session_id=None, active_job_for_turn=None`` and a channel that
does not drive job completion. The observer must not wire an exact
job-to-turn ``path_target_fn`` for them: a non-None callback that resolves
every turn to ``None`` would suppress the reducer's legacy oldest-running
heuristic and silently drop all file attribution for those participants.
Only registrations that actually carry an ``active_job_for_turn`` mapper get
exact attribution — the same capability check ``_live_completion_owned``
already applies to completion.
"""

from __future__ import annotations

from theater.daemon.jobs import JobManager
from theater.daemon.observation.live import LiveRegistration
from theater.daemon.observation.reducer import QuietClock
from theater.daemon.observation.service import Observer
from theater.daemon.observation.turns import TurnAccumulator
from theater.harness.contracts.channels import ChannelDeclaration, ChannelKind
from theater.harness.contracts.events import Event, EventKind, EventPath
from theater.harness.contracts.runtime import LiveChannelDeclaration
from theater.harness.source import Batch, Source

PASSIVE = LiveChannelDeclaration(
    channel=ChannelDeclaration(id="pi-frontend-live", kind=ChannelKind.LIVE),
    drives_job_completion=False,
)


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
    participant = registry.register(harness="pi", pane="%1", cwd=cwd)
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
