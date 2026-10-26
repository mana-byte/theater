"""Bus rows projected into trajectory records, and locating one exact record."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests.support.trajectory_service import (
    FakeBusStore,
    FakeObserver,
    FakeRegistry,
    snapshot_record,
    trajectory_participant,
)
from theater.constants.daemon import (
    BUS_KIND_JOB_AWAIT_END,
    BUS_KIND_JOB_AWAIT_START,
    BUS_KIND_PARTICIPANT_KILL_REQUESTED,
)
from theater.daemon.rpc.trajectory import _trajectory_locate
from theater.daemon.trajectory.service import TrajectoryService
from theater.daemon.trajectory.theater_events import project_bus_row
from theater.models import BadRequest
from theater.trajectory import (
    LinkDirection,
    TrajectoryKind,
)
from theater.trajectory.enums import TrajectoryFailureCategory
from theater.trajectory.location import TrajectoryLocationResolution


def test_bus_projection_is_allowlisted_and_directional():
    row = {
        "id": 7,
        "ts": 3.0,
        "from_id": "p",
        "to_id": "q",
        "kind": BUS_KIND_PARTICIPANT_KILL_REQUESTED,
        "payload": {},
    }
    left = project_bus_row(row, "p")
    right = project_bus_row(row, "q")
    assert left is not None and right is not None
    assert left.record_id == right.record_id == "bus:7"
    assert left.links[0].target_record_id == right.links[0].target_record_id == "bus:7"
    assert left.links[0].direction is LinkDirection.OUTGOING
    assert right.links[0].direction is LinkDirection.INCOMING
    assert project_bus_row({**row, "kind": "participant.status"}, "p") is None
    assert project_bus_row({**row, "kind": "agent.transcript"}, "p") is None


def test_bus_projection_links_counterparts_and_handle_chain_without_guessing() -> None:
    rows = (
        {
            "id": 17,
            "ts": 3.0,
            "from_id": "parent",
            "to_id": "child",
            "kind": "agent.send",
            "payload": {"handle": "child#2", "prompt": "work"},
        },
        {
            "id": 18,
            "ts": 4.0,
            "from_id": "parent",
            "to_id": "child",
            "kind": BUS_KIND_JOB_AWAIT_START,
            "payload": {"handle": "child#2", "token": "await-1"},
        },
        {
            "id": 19,
            "ts": 5.0,
            "from_id": "child",
            "to_id": "parent",
            "kind": "job.finished",
            "payload": {"handle": "child#2", "state": "done"},
        },
    )
    parent_records = tuple(project_bus_row(row, "parent") for row in rows)
    child_records = tuple(project_bus_row(row, "child") for row in rows)

    assert all(record is not None for record in parent_records)
    records = tuple(record for record in (*parent_records, *child_records) if record is not None)
    assert [record.kind for record in parent_records if record is not None] == [
        TrajectoryKind.SEND,
        TrajectoryKind.AWAIT_START,
        TrajectoryKind.RECEIVE,
    ]
    assert [record.kind for record in child_records if record is not None] == [
        TrajectoryKind.RECEIVE,
        TrajectoryKind.SEND,
    ]
    assert child_records[1] is None
    assert all(
        link.target_record_id == record.record_id for record in records for link in record.links
    )
    assert all(link.correlation_type == "job_handle" for record in records for link in record.links)
    assert all(link.correlation_key == "child#2" for record in records for link in record.links)


def test_bus_projection_keeps_spawn_resume_kill_and_failure_exact_without_fabricated_chain() -> (
    None
):
    rows = (
        ("participant.created", {}),
        ("participant.session_boundary", {"reason": "resume", "predecessor_id": "parent"}),
        (BUS_KIND_PARTICIPANT_KILL_REQUESTED, {}),
        ("job.finished", {"handle": "child#2", "state": "killed"}),
    )
    for row_id, (kind, payload) in enumerate(rows, start=40):
        parent = project_bus_row(
            {
                "id": row_id,
                "ts": 4.0,
                "from_id": "parent",
                "to_id": "child",
                "kind": kind,
                "payload": payload,
            },
            "parent",
        )
        child = project_bus_row(
            {
                "id": row_id,
                "ts": 4.0,
                "from_id": "parent",
                "to_id": "child",
                "kind": kind,
                "payload": payload,
            },
            "child",
        )
        assert parent is not None and child is not None
        assert (
            parent.links[0].target_record_id == child.links[0].target_record_id == f"bus:{row_id}"
        )
        if kind == "job.finished":
            assert parent.links[0].correlation_key == child.links[0].correlation_key == "child#2"
        else:
            assert parent.links[0].correlation_type is None


def test_locate_returns_warm_transcript_record_without_mutating_stream() -> None:
    participant = trajectory_participant("p")
    service = TrajectoryService(FakeBusStore(), FakeRegistry([participant]), FakeObserver())
    stream = service._runtime.create_stream(participant)
    record = snapshot_record("epoch:1:0")
    stream.ring.merge((record,))
    sequence = stream.ring.current_sequence
    last_used = stream.cache.last_used

    location = service.locate(participant.id, record.record_id)

    assert location.resolution is TrajectoryLocationResolution.EXACT
    assert location.record == record
    assert stream.ring.current_sequence == sequence
    assert stream.cache.last_used == last_used


def test_locate_reads_one_exact_allowlisted_bus_record_without_warming_stream() -> None:
    participant = trajectory_participant("p")
    store = FakeBusStore()
    store.rows.append(
        {
            "id": 71,
            "ts": 1.0,
            "from_id": participant.id,
            "to_id": "child",
            "kind": "agent.send",
            "payload": {"handle": "child#1", "prompt": "work"},
        }
    )
    service = TrajectoryService(store, FakeRegistry([participant]), FakeObserver())

    location = service.locate(participant.id, "bus:71")

    assert location.resolution is TrajectoryLocationResolution.EXACT
    assert location.record is not None and location.record.record_id == "bus:71"
    assert service.streams == {}


def test_locate_hides_wrong_participant_and_disallowed_bus_rows() -> None:
    participant = trajectory_participant("p")
    store = FakeBusStore()
    store.rows.extend(
        (
            {
                "id": 72,
                "ts": 1.0,
                "from_id": "other",
                "to_id": "child",
                "kind": "agent.send",
                "payload": {},
            },
            {
                "id": 73,
                "ts": 1.0,
                "from_id": participant.id,
                "to_id": "child",
                "kind": "participant.status",
                "payload": {},
            },
        )
    )
    service = TrajectoryService(store, FakeRegistry([participant]), FakeObserver())

    wrong = service.locate(participant.id, "bus:72")
    disallowed = service.locate(participant.id, "bus:73")

    assert wrong.resolution is TrajectoryLocationResolution.NOT_FOUND
    assert disallowed.resolution is TrajectoryLocationResolution.NOT_FOUND
    assert wrong.record is None and disallowed.record is None


def test_locate_distinguishes_missing_participant_and_cold_non_bus_record() -> None:
    participant = trajectory_participant("p")
    service = TrajectoryService(FakeBusStore(), FakeRegistry([participant]), FakeObserver())

    missing = service.locate("ghost", "bus:1")
    cold = service.locate(participant.id, "epoch:1:0")

    assert missing.resolution is TrajectoryLocationResolution.NOT_FOUND
    assert "participant" in missing.message
    assert cold.resolution is TrajectoryLocationResolution.UNAVAILABLE
    assert "trajectory.snapshot" in cold.message


def test_locate_does_not_query_for_malformed_bus_or_oversized_record_id() -> None:
    participant = trajectory_participant("p")
    store = FakeBusStore()
    service = TrajectoryService(store, FakeRegistry([participant]), FakeObserver())

    malformed = service.locate(participant.id, "bus:-1")

    assert malformed.resolution is TrajectoryLocationResolution.UNAVAILABLE
    assert store.record_lookups == []
    with pytest.raises(BadRequest, match="exceeds"):
        service.locate(participant.id, "x" * 513)


async def test_trajectory_locate_rpc_rejects_invalid_identifiers() -> None:
    daemon = SimpleNamespace(trajectory=SimpleNamespace(locate=lambda *_args: None))

    with pytest.raises(BadRequest, match="record_id"):
        await _trajectory_locate(daemon, {"id": "p", "record_id": ""})
    with pytest.raises(BadRequest, match="control characters"):
        await _trajectory_locate(daemon, {"id": "p", "record_id": "bus:\x00"})
    with pytest.raises(BadRequest, match="exceeds"):
        await _trajectory_locate(
            daemon,
            {"id": "p", "record_id": "x" * 513},
        )


def test_bus_projection_preserves_explicit_transcript_and_theater_failures() -> None:
    transcript = project_bus_row(
        {
            "id": 81,
            "ts": 10.0,
            "from_id": "p",
            "to_id": "p",
            "kind": "agent.observation_error",
            "payload": {"code": "transcript_identity_lost", "message": "identity changed"},
        },
        "p",
    )
    job = project_bus_row(
        {
            "id": 82,
            "ts": 11.0,
            "from_id": "p",
            "to_id": "q",
            "kind": "job.finished",
            "payload": {"state": "crashed", "error_code": "send_failed"},
        },
        "p",
    )
    timeout = project_bus_row(
        {
            "id": 83,
            "ts": 12.0,
            "from_id": "p",
            "to_id": "q",
            "kind": BUS_KIND_JOB_AWAIT_END,
            "payload": {"state": "timeout", "handle": "q#1"},
        },
        "p",
    )

    assert transcript is not None and transcript.failure is not None
    assert transcript.failure.category is TrajectoryFailureCategory.INCOMPLETE_TRANSCRIPT
    assert transcript.failure.code == "transcript_identity_lost"
    assert job is not None and job.failure is not None
    assert job.failure.category is TrajectoryFailureCategory.THEATER
    assert job.failure.code == "send_failed"
    assert timeout is not None and timeout.failure is not None
    assert timeout.failure.category is TrajectoryFailureCategory.TIMEOUT


def test_await_projection_uses_only_nonempty_handles_as_call_ids() -> None:
    start = project_bus_row(
        {
            "id": 9,
            "ts": 10.0,
            "from_id": "p",
            "to_id": "q",
            "kind": BUS_KIND_JOB_AWAIT_START,
            "payload": {"handle": "job-1"},
        },
        "p",
    )
    end = project_bus_row(
        {
            "id": 10,
            "ts": 11.0,
            "from_id": "p",
            "to_id": "q",
            "kind": BUS_KIND_JOB_AWAIT_END,
            "payload": {"handle": "job-1", "state": "completed"},
        },
        "p",
    )
    other = project_bus_row(
        {
            "id": 11,
            "ts": 12.0,
            "from_id": "p",
            "to_id": "q",
            "kind": BUS_KIND_PARTICIPANT_KILL_REQUESTED,
            "payload": {"handle": "job-1"},
        },
        "p",
    )

    assert start is not None and start.call_id == "job-1"
    assert end is not None and end.call_id == "job-1"
    assert other is not None and other.call_id is None
