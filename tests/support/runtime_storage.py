"""Row builders shared by the runtime-storage test modules."""

from __future__ import annotations

from theater.daemon.persistence.repositories.control_operations import ControlOperation
from theater.daemon.persistence.repositories.runtime_bindings import (
    ParticipantRuntimeBinding,
    encode_launch_policy,
)
from theater.harness.contracts.runtime import (
    ControlDeliveryPhase,
    ControlKind,
    ControlTransport,
    RuntimeLifecyclePhase,
    RuntimeWiring,
)
from theater.models import Job, JobState


def make_binding(participant_id: str = "p1", **overrides) -> ParticipantRuntimeBinding:
    values = {
        "participant_id": participant_id,
        "harness": "codex",
        "wiring": RuntimeWiring.NATIVE,
        "backend_generation": 1,
        "lifecycle": RuntimeLifecyclePhase.INTENDED,
        "endpoint": "unix:///tmp/private.sock",
        "launch_policy": encode_launch_policy({"approval": "on-request", "model": "m1"}),
        "created_at": 100.0,
        "updated_at": 100.0,
    }
    values.update(overrides)
    return ParticipantRuntimeBinding(**values)


def make_operation(operation_id: str, participant_id: str = "p1", **overrides) -> ControlOperation:
    values = {
        "operation_id": operation_id,
        "participant_id": participant_id,
        "kind": ControlKind.SEND,
        "transport": ControlTransport.NATIVE_RUNTIME,
        "delivery_phase": ControlDeliveryPhase.RESERVED,
        "created_at": 100.0,
        "updated_at": 100.0,
    }
    values.update(overrides)
    return ControlOperation(**values)


def make_job(handle: str, created_at: float = 100.0, target_id: str = "p1") -> Job:
    return Job(
        handle=handle,
        caller_id="caller",
        target_id=target_id,
        kind="send",
        prompt=None,
        state=JobState.RUNNING,
        result=None,
        error_code=None,
        created_at=created_at,
        finished_at=None,
    )
