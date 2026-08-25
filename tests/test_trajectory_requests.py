"""Focused tests for canonical trajectory request projections."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from theater.constants.trajectory import (
    TRAJECTORY_IDENTIFIER_MAX_BYTES,
    TRAJECTORY_REQUEST_RECORD_LIMIT,
)
from theater.daemon.trajectory.overview import capabilities_for, overview_for
from theater.harness.builtin.plugins.claude import ClaudeCodeObserver
from theater.harness.builtin.plugins.codex import CodexObserver
from theater.harness.builtin.plugins.opencode import OpenCodeSource
from theater.harness.builtin.plugins.vibe import VibeObserver
from theater.harness.contracts.events import Event, EventKind, TokenUsage
from theater.harness.contracts.trajectory import TrajectoryFact
from theater.trajectory import (
    Timing,
    TimingProvenance,
    TrajectoryCapabilities,
    TrajectoryFeature,
    TrajectoryKind,
    TrajectoryLane,
    TrajectoryRecord,
    TrajectoryRequest,
    TrajectoryRequestIdentity,
    TrajectoryStatus,
    TrajectoryUsage,
    TrajectoryValidationError,
    event_to_fact,
    fact_to_record,
    requests_for_records,
)
from theater.trajectory.enums import CostProvenance, TrajectoryFailureCategory
from theater.trajectory.records import TrajectoryFailure

FIXTURES = Path(__file__).parent / "fixtures"


def _record(
    record_id: str,
    index: int,
    *,
    participant_id: str = "participant",
    source_epoch: str = "epoch",
    source: str = "test",
    lane: TrajectoryLane = TrajectoryLane.MODEL,
    kind: TrajectoryKind = TrajectoryKind.ASSISTANT,
    status: TrajectoryStatus = TrajectoryStatus.COMPLETED,
    revision: int = 0,
    request_id: str | None = None,
    usage: TrajectoryUsage | None = None,
    turn_id: str | None = None,
    step_id: str | None = None,
    timing: Timing | None = None,
    failure: TrajectoryFailure | None = None,
    retry_of_record_id: str | None = None,
    retry_attempt: int | None = None,
) -> TrajectoryRecord:
    return TrajectoryRecord(
        record_id=record_id,
        revision=revision,
        participant_id=participant_id,
        source_epoch=source_epoch,
        lane=lane,
        kind=kind,
        source=source,
        summary=record_id,
        status=status,
        raw_index=index,
        request_id=request_id,
        usage=usage,
        turn_id=turn_id,
        step_id=step_id,
        timing=timing,
        failure=failure,
        retry_of_record_id=retry_of_record_id,
        retry_attempt=retry_attempt,
    )


def _fixture_facts(observer, name: str):
    return [
        fact
        for index, line in enumerate((FIXTURES / name).read_text(encoding="utf-8").splitlines())
        for fact in observer.parse_record(line, index).trajectory
    ]


def test_record_request_id_is_additive_and_fact_projection_preserves_it() -> None:
    record = _record("record", 1, request_id="request")
    old_wire = record.to_wire()
    old_wire.pop("request_id")

    assert TrajectoryRecord.from_wire(old_wire).request_id is None
    assert TrajectoryRecord.from_wire(record.to_wire()) == record
    with pytest.raises(TrajectoryValidationError):
        _record("control", 1, request_id="bad\x00")
    with pytest.raises(TrajectoryValidationError):
        _record("oversize", 1, request_id="x" * (TRAJECTORY_IDENTIFIER_MAX_BYTES + 1))

    fact = TrajectoryFact(kind=TrajectoryKind.ASSISTANT, request_id="explicit")
    assert (
        fact_to_record(fact, participant_id="participant", source_epoch="epoch").request_id
        == "explicit"
    )


def test_baseline_event_usage_is_a_conservative_request_association() -> None:
    fact = event_to_fact(
        Event(
            kind=EventKind.ASSISTANT,
            usage=TokenUsage(idempotency_key="usage-request"),
        )
    )

    assert fact.request_id is None
    assert fact.usage is not None and fact.usage.request_id == "usage-request"
    record = fact_to_record(fact, participant_id="participant", source_epoch="epoch")
    request = requests_for_records((record,))[0]
    assert request.identity is TrajectoryRequestIdentity.USAGE
    assert request.source_request_id == "usage-request"


def test_shared_request_groups_are_scoped_by_participant_and_epoch() -> None:
    requests = requests_for_records(
        (
            _record("first", 1, request_id="shared"),
            _record("second", 2, request_id="shared"),
            _record("other-epoch", 1, source_epoch="other", request_id="shared"),
            _record("other-participant", 3, participant_id="other", request_id="shared"),
        )
    )

    assert [request.record_ids for request in requests] == [
        ("first", "second"),
        ("other-participant",),
        ("other-epoch",),
    ]
    assert len({request.request_id for request in requests}) == 3


def test_explicit_identity_beats_usage_and_requestless_model_usage_stands_alone() -> None:
    requests = requests_for_records(
        (
            _record("usage", 1, usage=TrajectoryUsage(request_id="shared")),
            _record("explicit", 2, request_id="shared"),
            _record("local", 3, usage=TrajectoryUsage(model="local")),
            _record("tool", 4, lane=TrajectoryLane.TOOLS, usage=TrajectoryUsage(model="tool")),
            _record("plain", 5),
        )
    )

    assert [(request.identity, request.record_ids) for request in requests] == [
        (TrajectoryRequestIdentity.SOURCE, ("usage", "explicit")),
        (TrajectoryRequestIdentity.RECORD, ("local",)),
    ]
    assert requests[0].source_request_id == "shared"
    assert requests[1].source_request_id is None


def test_shared_request_id_survives_provenance_upgrade_and_avoids_record_fallback() -> None:
    usage = _record("usage", 1, usage=TrajectoryUsage(request_id="shared"))
    explicit = _record("explicit", 2, request_id="shared")
    record_local = _record("shared", 3, usage=TrajectoryUsage(model="local"))

    usage_request = requests_for_records((usage,))[0]
    upgraded_request = requests_for_records((usage, explicit))[0]
    local_request = requests_for_records((usage, record_local))[1]

    assert usage_request.identity is TrajectoryRequestIdentity.USAGE
    assert upgraded_request.identity is TrajectoryRequestIdentity.SOURCE
    assert upgraded_request.request_id == usage_request.request_id
    assert local_request.identity is TrajectoryRequestIdentity.RECORD
    assert local_request.request_id != usage_request.request_id


def test_request_projection_uses_latest_usage_and_consistent_turn_step_values() -> None:
    request = requests_for_records(
        (
            _record(
                "first",
                1,
                request_id="request",
                usage=TrajectoryUsage(model="old", input_tokens=1),
                turn_id="turn",
                step_id="one",
            ),
            _record(
                "latest",
                2,
                request_id="request",
                usage=TrajectoryUsage(model="new", input_tokens=9),
                turn_id="turn",
                step_id="two",
            ),
        )
    )[0]

    assert request.usage == TrajectoryUsage(model="new", input_tokens=9)
    assert request.model == "new"
    assert request.turn_id == "turn"
    assert request.step_id is None


def test_request_exposes_provenance_timing_failure_retry_and_record_associations() -> None:
    usage = TrajectoryUsage(
        model="model-x",
        provider="provider-x",
        output_tokens=200,
        cost_usd=0.5,
        cost_provenance=CostProvenance.REPORTED,
    )
    records = (
        _record(
            "context",
            1,
            request_id="request",
            kind=TrajectoryKind.CONTEXT,
            timing=Timing(start=10.0, provenance=TimingProvenance.SOURCE),
        ),
        _record(
            "model",
            2,
            request_id="request",
            status=TrajectoryStatus.ERROR,
            usage=usage,
            timing=Timing(
                start=10.0,
                first_token=10.25,
                end=11.25,
                provenance=TimingProvenance.SOURCE,
            ),
            failure=TrajectoryFailure(
                TrajectoryFailureCategory.TRANSPORT,
                code="disconnected",
                detail="connection closed",
            ),
            retry_of_record_id="prior-request-record",
            retry_attempt=2,
        ),
        _record(
            "tool",
            3,
            request_id="request",
            lane=TrajectoryLane.TOOLS,
            kind=TrajectoryKind.TOOL_RESULT,
        ),
        _record(
            "coordination",
            4,
            request_id="request",
            lane=TrajectoryLane.THEATER,
            kind=TrajectoryKind.SEND,
            status=TrajectoryStatus.ERROR,
        ),
    )

    request = requests_for_records(records)[0]

    assert (request.provider, request.model) == ("provider-x", "model-x")
    assert request.ttft_ms == 250
    assert request.generation_duration_ms == 1000
    assert request.output_tokens_per_second == 200
    assert request.failure == records[1].failure
    assert (request.retry_of_record_id, request.retry_attempt) == ("prior-request-record", 2)
    assert request.context_record_ids == ("context",)
    assert request.model_record_ids == ("model",)
    assert request.tool_record_ids == ("tool",)
    assert request.coordination_record_ids == ("coordination",)
    assert TrajectoryRequest.from_wire(request.to_wire()) == request


def test_request_diagnostics_use_model_timing_not_context_or_tool_bounds() -> None:
    records = (
        _record(
            "context",
            1,
            request_id="request",
            kind=TrajectoryKind.CONTEXT,
            timing=Timing(start=8.0, end=9.0, provenance=TimingProvenance.SOURCE),
        ),
        _record(
            "model",
            2,
            request_id="request",
            usage=TrajectoryUsage(output_tokens=100),
            timing=Timing(
                start=10.0,
                first_token=10.2,
                end=11.2,
                provenance=TimingProvenance.SOURCE,
            ),
        ),
        _record(
            "tool",
            3,
            request_id="request",
            lane=TrajectoryLane.TOOLS,
            kind=TrajectoryKind.TOOL_RESULT,
            status=TrajectoryStatus.ERROR,
            timing=Timing(start=11.3, end=20.0, provenance=TimingProvenance.SOURCE),
            failure=TrajectoryFailure(TrajectoryFailureCategory.TOOL, detail="failed"),
        ),
    )

    request = requests_for_records(records)[0]

    assert request.status is TrajectoryStatus.COMPLETED
    assert request.timing is not None
    assert request.timing.start == 10.0
    assert request.timing.first_token == 10.2
    assert request.timing.end == 11.2
    assert request.timing.duration_ms == pytest.approx(1200.0)
    assert request.timing.provenance is TimingProvenance.DERIVED
    assert request.failure is None
    assert request.output_tokens_per_second == pytest.approx(100.0)


def test_request_projection_is_deterministic_across_duplicate_revisions() -> None:
    records = (
        _record("later", 2, request_id="later"),
        _record("first", 1, request_id="first"),
        _record("duplicate", 3, request_id="old", revision=1),
        _record("duplicate", 3, request_id="new", revision=2),
    )

    requests = requests_for_records(records)

    assert [request.source_request_id for request in requests] == ["first", "later", "new"]
    assert [request.record_ids for request in requests] == [("first",), ("later",), ("duplicate",)]


def test_canonical_request_ids_are_bounded_and_stable() -> None:
    record = _record(
        "record",
        1,
        participant_id="p" * TRAJECTORY_IDENTIFIER_MAX_BYTES,
        source_epoch="e" * TRAJECTORY_IDENTIFIER_MAX_BYTES,
        request_id="r" * TRAJECTORY_IDENTIFIER_MAX_BYTES,
    )

    first = requests_for_records((record,))[0]
    second = requests_for_records((record,))[0]

    assert first.request_id == second.request_id
    assert len(first.request_id.encode("utf-8")) <= TRAJECTORY_IDENTIFIER_MAX_BYTES


def test_request_timing_uses_active_and_terminal_rules() -> None:
    active = requests_for_records(
        (
            _record(
                "start",
                1,
                request_id="active",
                status=TrajectoryStatus.PENDING,
                timing=Timing(start=2, end=3, duration_ms=1000, provenance=TimingProvenance.SOURCE),
            ),
            _record(
                "latest",
                2,
                request_id="active",
                status=TrajectoryStatus.RUNNING,
                timing=Timing(start=1, duration_ms=400, provenance=TimingProvenance.SOURCE),
            ),
        )
    )[0]
    terminal = requests_for_records(
        (
            _record(
                "start",
                1,
                request_id="terminal",
                timing=Timing(start=2, provenance=TimingProvenance.SOURCE),
            ),
            _record(
                "end",
                2,
                request_id="terminal",
                timing=Timing(start=3, end=7, provenance=TimingProvenance.SOURCE),
            ),
        )
    )[0]

    assert active.timing == Timing(start=1, provenance=TimingProvenance.SOURCE)
    assert terminal.timing == Timing(
        start=2,
        end=7,
        duration_ms=5000,
        provenance=TimingProvenance.DERIVED,
    )


def test_request_timing_falls_back_when_cross_record_clocks_contradict() -> None:
    request = requests_for_records(
        (
            _record(
                "start",
                1,
                request_id="request",
                timing=Timing(start=10, provenance=TimingProvenance.SOURCE),
            ),
            _record(
                "end",
                2,
                request_id="request",
                timing=Timing(end=5, provenance=TimingProvenance.SOURCE),
            ),
        )
    )[0]

    assert request.timing == Timing(end=5, provenance=TimingProvenance.SOURCE)


def test_request_timing_uses_terminal_point_and_marks_observed_estimate() -> None:
    source = requests_for_records(
        (
            _record(
                "start",
                1,
                request_id="source-points",
                status=TrajectoryStatus.RUNNING,
                timing=Timing(start=10, provenance=TimingProvenance.SOURCE),
            ),
            _record(
                "end",
                2,
                request_id="source-points",
                timing=Timing(start=12, provenance=TimingProvenance.SOURCE),
            ),
        )
    )[0]
    observed = requests_for_records(
        (
            _record(
                "observed-start",
                1,
                request_id="observed-points",
                status=TrajectoryStatus.RUNNING,
                timing=Timing(start=20, provenance=TimingProvenance.OBSERVED),
            ),
            _record(
                "observed-end",
                2,
                request_id="observed-points",
                timing=Timing(end=23, provenance=TimingProvenance.OBSERVED),
            ),
        )
    )[0]

    assert source.timing == Timing(
        start=10,
        end=12,
        duration_ms=2_000,
        provenance=TimingProvenance.DERIVED,
    )
    assert observed.timing == Timing(
        start=20,
        end=23,
        duration_ms=3_000,
        provenance=TimingProvenance.OBSERVED,
    )


def test_request_record_links_keep_newest_ids_and_aggregate_full_group() -> None:
    records = tuple(
        _record(
            f"record-{index}",
            index,
            request_id="large",
            usage=TrajectoryUsage(model="latest", input_tokens=index)
            if index == TRAJECTORY_REQUEST_RECORD_LIMIT
            else None,
            timing=(
                Timing(start=1, provenance=TimingProvenance.SOURCE)
                if index == 0
                else Timing(end=3, provenance=TimingProvenance.SOURCE)
                if index == TRAJECTORY_REQUEST_RECORD_LIMIT
                else None
            ),
        )
        for index in range(TRAJECTORY_REQUEST_RECORD_LIMIT + 1)
    )

    request = requests_for_records(records)[0]

    assert request.records_truncated
    assert request.record_ids == tuple(
        f"record-{index}" for index in range(1, TRAJECTORY_REQUEST_RECORD_LIMIT + 1)
    )
    assert request.usage == TrajectoryUsage(
        model="latest", input_tokens=TRAJECTORY_REQUEST_RECORD_LIMIT
    )
    assert request.timing == Timing(
        start=1,
        end=3,
        duration_ms=2000,
        provenance=TimingProvenance.DERIVED,
    )


def test_request_wire_is_strict_and_round_trips() -> None:
    request = TrajectoryRequest(
        request_id="request",
        participant_id="participant",
        source_epoch="epoch",
        source="source",
        record_ids=("record",),
        identity=TrajectoryRequestIdentity.SOURCE,
        source_request_id="source-request",
        timing=Timing(start=1, end=2, duration_ms=1000, provenance=TimingProvenance.SOURCE),
        usage=TrajectoryUsage(model="model", input_tokens=1),
    )
    invalid = request.to_wire()
    invalid["extra"] = True

    assert TrajectoryRequest.from_wire(request.to_wire()) == request
    with pytest.raises(TrajectoryValidationError):
        TrajectoryRequest.from_wire(invalid)
    with pytest.raises(TrajectoryValidationError):
        TrajectoryRequest(
            request_id="request",
            participant_id="participant",
            source_epoch="epoch",
            source="source",
            record_ids=("record", "record"),
            identity=TrajectoryRequestIdentity.SOURCE,
        )


def test_supported_plugins_expose_only_stable_request_identity() -> None:
    claude = _fixture_facts(ClaudeCodeObserver(), "trajectory_claude.jsonl")
    assistant = [
        fact
        for fact in claude
        if fact.kind
        in {TrajectoryKind.ASSISTANT, TrajectoryKind.REASONING, TrajectoryKind.TOOL_CALL}
        and fact.raw_index == 1
    ]
    user_result = next(fact for fact in claude if fact.summary == "file contents")
    codex = _fixture_facts(CodexObserver(), "trajectory_codex.jsonl")
    codex_assistant = next(fact for fact in codex if fact.summary == "I will make the change.")
    session = next(fact for fact in codex if fact.summary == "session metadata")
    source = OpenCodeSource(Path("unused"), cwd=None)
    opencode = source._stored_facts_for_message(
        {
            "id": "message",
            "role": "assistant",
            "finish": "stop",
            "tokens": {"input": 1},
        },
        (("part", 1, 1, json.dumps({"id": "part", "type": "reasoning", "text": "why"})),),
        raw_index=1,
        message_revision=0,
    )
    vibe = VibeObserver().parse_record(
        (FIXTURES / "vibe_messages.jsonl").read_text(encoding="utf-8").splitlines()[2], 2
    )

    assert {fact.request_id for fact in assistant} == {"request-1"}
    assert user_result.request_id is None
    assert codex_assistant.request_id == "turn-1"
    assert session.request_id is None
    assert {fact.request_id for fact in opencode} == {"opencode:message"}
    assert all(fact.request_id is None for fact in vibe.trajectory)
    assert TrajectoryFeature.REQUESTS in VibeObserver.trajectory_capabilities.unsupported


def test_overview_counts_the_same_canonical_requests() -> None:
    overview = overview_for(
        (
            _record("one", 1, request_id="same", usage=TrajectoryUsage(input_tokens=1)),
            _record("two", 2, request_id="same", usage=TrajectoryUsage(input_tokens=2)),
            _record("three", 3, usage=TrajectoryUsage(input_tokens=3)),
            _record("four", 4, source_epoch="other", request_id="same"),
        ),
        has_older=False,
        has_coverage_gaps=False,
    )

    assert overview.model_operations == 3


def test_explicit_request_id_observes_request_capability_without_usage() -> None:
    capabilities = capabilities_for(
        TrajectoryCapabilities(),
        (_record("explicit", 1, request_id="request"),),
        live_updates_observed=False,
    )

    assert TrajectoryFeature.REQUESTS in capabilities.observed
