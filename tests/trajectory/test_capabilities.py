"""Trajectory capability declarations and their compact wire form."""

from __future__ import annotations

import pytest

from tests.support.trajectory_overview import overview_record
from theater.daemon.trajectory.aggregation import capabilities_for
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
    TrajectorySupport,
    TrajectoryUsage,
    TrajectoryValidationError,
)


def test_legacy_page_and_delta_decode_with_safe_usefulness_defaults() -> None:
    page = TrajectoryPage.from_wire({"panel_state": {"state": "ready"}})
    delta = TrajectoryDelta.from_wire({"stream_id": "stream"})

    assert page.capabilities == TrajectoryCapabilities()
    assert not page.overview.scope_complete
    assert page.overview.incomplete_reasons == (TrajectoryIncompleteReason.UNKNOWN,)
    empty = TrajectoryPage.from_wire({"panel_state": {"state": "ready"}, "overview": {}})
    assert not empty.overview.scope_complete
    assert empty.overview.incomplete_reasons == (TrajectoryIncompleteReason.UNKNOWN,)
    assert delta.capabilities is None
    assert delta.overview is None


def test_capabilities_keep_declaration_separate_from_observed_records() -> None:
    declared = TrajectoryCapabilities(
        supported=frozenset({TrajectoryFeature.MODELS, TrajectoryFeature.TOOLS}),
        unsupported=frozenset({TrajectoryFeature.RETRIES}),
    )
    records = (
        overview_record("tool", 1, kind=TrajectoryKind.TOOL_CALL, lane=TrajectoryLane.TOOLS),
        overview_record("model", 2, usage=TrajectoryUsage(model="model")),
    )

    capabilities = capabilities_for(declared, records, live_updates_observed=True)
    assert capabilities.support_for(TrajectoryFeature.MODELS) is TrajectorySupport.SUPPORTED
    assert TrajectoryFeature.MODELS in capabilities.observed
    assert TrajectoryFeature.TOOLS in capabilities.observed
    assert capabilities.support_for(TrajectoryFeature.RETRIES) is TrajectorySupport.UNSUPPORTED
    assert TrajectoryFeature.RETRIES not in capabilities.observed
    assert capabilities.support_for(TrajectoryFeature.CONTEXT) is TrajectorySupport.UNKNOWN
    assert TrajectoryFeature.LIVE_UPDATES in capabilities.observed


def test_capabilities_observe_explicit_first_token_and_retry_facts() -> None:
    records = (
        overview_record(
            "timing",
            1,
            timing=Timing(first_token=2.0, provenance=TimingProvenance.SOURCE),
        ),
        overview_record("retry", 2, retry_of_record_id="timing"),
    )

    capabilities = capabilities_for(
        TrajectoryCapabilities(),
        records,
        live_updates_observed=False,
    )

    assert TrajectoryFeature.TIMING in capabilities.observed
    assert TrajectoryFeature.RETRIES in capabilities.observed


def test_capability_wire_is_compact_and_deterministic() -> None:
    capabilities = TrajectoryCapabilities(
        supported=frozenset({TrajectoryFeature.TOOLS, TrajectoryFeature.MODELS}),
        unsupported=frozenset({TrajectoryFeature.RETRIES}),
        observed=frozenset({TrajectoryFeature.TOOLS}),
    )

    assert capabilities.to_wire() == {
        "supported": ["models", "tools"],
        "unsupported": ["retries"],
        "observed": ["tools"],
    }
    assert TrajectoryCapabilities.from_wire(capabilities.to_wire()) == capabilities


def test_capability_wire_remains_strict() -> None:
    with pytest.raises(TrajectoryValidationError):
        TrajectoryCapabilities.from_wire({"supported": [], "extra": True})
    with pytest.raises(TrajectoryValidationError):
        TrajectoryCapabilities.from_wire({"supported": ["models"], "unsupported": ["models"]})
    with pytest.raises(TrajectoryValidationError):
        TrajectoryCapabilities.from_wire({"observed": ["models", "models"]})
