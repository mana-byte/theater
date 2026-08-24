"""Capability and loaded-scope trajectory backend values."""

from __future__ import annotations

import pytest

from theater.daemon.trajectory.overview import capabilities_for, overview_for
from theater.trajectory import (
    Timing,
    TimingProvenance,
    TrajectoryCapabilities,
    TrajectoryDelta,
    TrajectoryFeature,
    TrajectoryIncompleteReason,
    TrajectoryKind,
    TrajectoryLane,
    TrajectoryPage,
    TrajectoryRecord,
    TrajectoryStatus,
    TrajectorySupport,
    TrajectoryUsage,
    TrajectoryValidationError,
)


def _record(
    record_id: str,
    index: int,
    *,
    source_epoch: str = "epoch",
    kind: TrajectoryKind = TrajectoryKind.ASSISTANT,
    lane: TrajectoryLane = TrajectoryLane.MODEL,
    status: TrajectoryStatus = TrajectoryStatus.COMPLETED,
    call_id: str | None = None,
    usage: TrajectoryUsage | None = None,
    timing: Timing | None = None,
) -> TrajectoryRecord:
    return TrajectoryRecord(
        record_id=record_id,
        revision=0,
        participant_id="participant",
        source_epoch=source_epoch,
        lane=lane,
        kind=kind,
        source="test",
        summary=record_id,
        status=status,
        raw_index=index,
        call_id=call_id,
        usage=usage,
        timing=timing,
    )


def test_legacy_page_and_delta_decode_with_safe_usefulness_defaults() -> None:
    page = TrajectoryPage.from_wire({"panel_state": {"state": "ready"}})
    delta = TrajectoryDelta.from_wire({"stream_id": "stream"})

    assert page.capabilities == TrajectoryCapabilities()
    assert page.overview.scope.value == "daemon_cache"
    assert page.overview.scope_complete
    assert delta.capabilities is None
    assert delta.overview is None


def test_capabilities_keep_declaration_separate_from_observed_records() -> None:
    declared = TrajectoryCapabilities.declared(
        supported=frozenset({TrajectoryFeature.MODELS, TrajectoryFeature.TOOLS}),
        unsupported=frozenset({TrajectoryFeature.RETRIES}),
    )
    records = (
        _record("tool", 1, kind=TrajectoryKind.TOOL_CALL, lane=TrajectoryLane.TOOLS),
        _record("model", 2, usage=TrajectoryUsage(model="model")),
    )

    capabilities = capabilities_for(declared, records, live_updates_observed=True)
    by_feature = {item.feature: item for item in capabilities.features}

    assert by_feature[TrajectoryFeature.MODELS].declared is TrajectorySupport.SUPPORTED
    assert by_feature[TrajectoryFeature.MODELS].observed
    assert by_feature[TrajectoryFeature.TOOLS].observed
    assert by_feature[TrajectoryFeature.RETRIES].declared is TrajectorySupport.UNSUPPORTED
    assert not by_feature[TrajectoryFeature.RETRIES].observed
    assert by_feature[TrajectoryFeature.CONTEXT].declared is TrajectorySupport.UNKNOWN
    assert by_feature[TrajectoryFeature.LIVE_UPDATES].observed


@pytest.mark.parametrize(
    ("records", "expected"),
    [
        (
            (
                _record(
                    "call",
                    1,
                    kind=TrajectoryKind.TOOL_CALL,
                    lane=TrajectoryLane.TOOLS,
                    status=TrajectoryStatus.RUNNING,
                    call_id="call",
                ),
                _record(
                    "result",
                    2,
                    kind=TrajectoryKind.TOOL_RESULT,
                    lane=TrajectoryLane.TOOLS,
                    call_id="call",
                ),
            ),
            None,
        ),
        (
            (
                _record(
                    "call",
                    1,
                    kind=TrajectoryKind.TOOL_CALL,
                    lane=TrajectoryLane.TOOLS,
                    status=TrajectoryStatus.RUNNING,
                    call_id="call",
                ),
                _record(
                    "result",
                    2,
                    kind=TrajectoryKind.TOOL_RESULT,
                    lane=TrajectoryLane.TOOLS,
                    call_id="other",
                ),
            ),
            "call",
        ),
        (
            (
                _record(
                    "call",
                    1,
                    kind=TrajectoryKind.TOOL_CALL,
                    lane=TrajectoryLane.TOOLS,
                    status=TrajectoryStatus.RUNNING,
                    call_id="call",
                ),
                _record(
                    "result",
                    2,
                    source_epoch="other",
                    kind=TrajectoryKind.TOOL_RESULT,
                    lane=TrajectoryLane.TOOLS,
                    call_id="call",
                ),
            ),
            "call",
        ),
        (
            (
                _record(
                    "result",
                    1,
                    kind=TrajectoryKind.TOOL_RESULT,
                    lane=TrajectoryLane.TOOLS,
                    call_id="call",
                ),
                _record(
                    "call",
                    2,
                    kind=TrajectoryKind.TOOL_CALL,
                    lane=TrajectoryLane.TOOLS,
                    status=TrajectoryStatus.RUNNING,
                    call_id="call",
                ),
            ),
            "call",
        ),
    ],
)
def test_current_tool_call_needs_a_later_exact_terminal_result(
    records: tuple[TrajectoryRecord, ...], expected: str | None
) -> None:
    overview = overview_for(records, has_older=False, has_coverage_gaps=False)

    assert (overview.current.record_id if overview.current is not None else None) == expected


def test_usage_uses_latest_explicit_request_and_conservative_implicit_model_records() -> None:
    records = (
        _record(
            "first",
            1,
            usage=TrajectoryUsage(request_id="request", input_tokens=1, cost_usd=0.1),
        ),
        _record(
            "implicit-1",
            2,
            usage=TrajectoryUsage(input_tokens=2, cost_usd=0.2),
        ),
        _record(
            "latest",
            3,
            usage=TrajectoryUsage(request_id="request", input_tokens=4, cost_usd=0.4),
        ),
        _record(
            "implicit-2",
            4,
            usage=TrajectoryUsage(input_tokens=8, cost_usd=0.8),
        ),
    )

    overview = overview_for(records, has_older=False, has_coverage_gaps=False)

    assert overview.model_operations == 3
    assert overview.input_tokens == 14
    assert overview.reported_cost_usd == pytest.approx(1.4)


def test_totals_saturate_and_latest_problem_includes_failure_statuses_and_kinds() -> None:
    records = (
        _record("timeout", 1, status=TrajectoryStatus.TIMEOUT),
        _record("job", 2, kind=TrajectoryKind.JOB_FAILURE),
        _record(
            "large-1",
            3,
            usage=TrajectoryUsage(input_tokens=(1 << 63) - 1, cost_usd=1e15),
        ),
        _record("large-2", 4, usage=TrajectoryUsage(input_tokens=1, cost_usd=1.0)),
    )

    overview = overview_for(records, has_older=False, has_coverage_gaps=False)

    assert overview.input_tokens == (1 << 63) - 1
    assert overview.reported_cost_usd == 1e15
    assert overview.totals_saturated
    assert overview.latest_problem is not None
    assert overview.latest_problem.record_id == "job"


@pytest.mark.parametrize(
    "status",
    (
        TrajectoryStatus.ERROR,
        TrajectoryStatus.INTERRUPTED,
        TrajectoryStatus.TIMEOUT,
        TrajectoryStatus.CANCELLED,
    ),
)
def test_latest_problem_includes_terminal_failure_statuses(status: TrajectoryStatus) -> None:
    overview = overview_for(
        (_record(status.value, 1, status=status),),
        has_older=False,
        has_coverage_gaps=False,
    )

    assert overview.latest_problem is not None
    assert overview.latest_problem.record_id == status.value


@pytest.mark.parametrize(
    ("has_older", "has_coverage_gaps", "cache_evicted", "expected"),
    [
        (False, False, False, ()),
        (True, False, False, (TrajectoryIncompleteReason.OLDER_HISTORY,)),
        (False, True, False, (TrajectoryIncompleteReason.COVERAGE_GAPS,)),
        (False, False, True, (TrajectoryIncompleteReason.CACHE_EVICTED,)),
    ],
)
def test_scope_completeness_reports_each_daemon_cache_limit(
    has_older: bool,
    has_coverage_gaps: bool,
    cache_evicted: bool,
    expected: tuple[TrajectoryIncompleteReason, ...],
) -> None:
    overview = overview_for(
        (),
        has_older=has_older,
        has_coverage_gaps=has_coverage_gaps,
        cache_evicted=cache_evicted,
    )

    assert overview.incomplete_reasons == expected
    assert overview.scope_complete == (not expected)


def test_loaded_scope_overview_uses_canonical_order_and_exact_facts() -> None:
    records = (
        _record("error", 2, kind=TrajectoryKind.ERROR, status=TrajectoryStatus.ERROR),
        _record(
            "active",
            3,
            status=TrajectoryStatus.RUNNING,
            usage=TrajectoryUsage(
                model="gpt",
                input_tokens=4,
                output_tokens=5,
                reasoning_tokens=6,
                cache_read_tokens=7,
                cache_write_tokens=8,
                cost_usd=0.25,
            ),
            timing=Timing(start=1.0, duration_ms=20.0, provenance=TimingProvenance.SOURCE),
        ),
        _record("older", 1, status=TrajectoryStatus.PENDING),
    )

    overview = overview_for(records, has_older=True, has_coverage_gaps=True)

    assert overview.scope.value == "daemon_cache"
    assert not overview.scope_complete
    assert overview.incomplete_reasons == (
        TrajectoryIncompleteReason.OLDER_HISTORY,
        TrajectoryIncompleteReason.COVERAGE_GAPS,
    )
    assert overview.record_count == 3
    assert overview.model_operations == 1
    assert overview.input_tokens == 4
    assert overview.output_tokens == 5
    assert overview.cache_read_tokens == 7
    assert overview.cache_write_tokens == 8
    assert overview.reasoning_tokens == 6
    assert overview.reported_cost_usd == 0.25
    assert overview.current is not None and overview.current.record_id == "active"
    assert overview.current.model == "gpt"
    assert overview.latest_problem is not None and overview.latest_problem.record_id == "error"


def test_capability_wire_remains_strict() -> None:
    with pytest.raises(TrajectoryValidationError):
        TrajectoryCapabilities.from_wire({"features": [], "extra": True})
