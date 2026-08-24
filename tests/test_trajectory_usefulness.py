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
    kind: TrajectoryKind = TrajectoryKind.ASSISTANT,
    lane: TrajectoryLane = TrajectoryLane.MODEL,
    status: TrajectoryStatus = TrajectoryStatus.COMPLETED,
    usage: TrajectoryUsage | None = None,
    timing: Timing | None = None,
) -> TrajectoryRecord:
    return TrajectoryRecord(
        record_id=record_id,
        revision=0,
        participant_id="participant",
        source_epoch="epoch",
        lane=lane,
        kind=kind,
        source="test",
        summary=record_id,
        status=status,
        raw_index=index,
        usage=usage,
        timing=timing,
    )


def test_legacy_page_and_delta_decode_with_safe_usefulness_defaults() -> None:
    page = TrajectoryPage.from_wire({"panel_state": {"state": "ready"}})
    delta = TrajectoryDelta.from_wire({"stream_id": "stream"})

    assert page.capabilities == TrajectoryCapabilities()
    assert page.overview.record_count == 0
    assert delta.capabilities is None
    assert delta.overview is None


def test_capabilities_keep_declaration_separate_from_observed_records() -> None:
    declared = TrajectoryCapabilities.declared(
        supported=frozenset({TrajectoryFeature.MODELS, TrajectoryFeature.TOOLS}),
        unsupported=frozenset({TrajectoryFeature.RETRIES}),
    )
    records = (
        _record(
            "tool",
            1,
            kind=TrajectoryKind.TOOL_CALL,
            lane=TrajectoryLane.TOOLS,
        ),
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


def test_loaded_scope_overview_uses_canonical_order_and_exact_facts() -> None:
    records = (
        _record(
            "error",
            2,
            kind=TrajectoryKind.ERROR,
            status=TrajectoryStatus.ERROR,
        ),
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

    assert overview.scope.value == "loaded"
    assert not overview.scope_complete
    assert overview.has_older and overview.has_coverage_gaps
    assert overview.record_count == 3
    assert overview.model_operations == 3
    assert overview.tool_operations == 0
    assert overview.input_tokens == 4
    assert overview.output_tokens == 5
    assert overview.cache_read_tokens == 7
    assert overview.cache_write_tokens == 8
    assert overview.reasoning_tokens == 6
    assert overview.reported_cost_usd == 0.25
    assert overview.current is not None and overview.current.record_id == "active"
    assert overview.current.model == "gpt"
    assert overview.latest_error is not None and overview.latest_error.record_id == "error"


def test_capability_wire_remains_strict() -> None:
    with pytest.raises(TrajectoryValidationError):
        TrajectoryCapabilities.from_wire({"features": [], "extra": True})
