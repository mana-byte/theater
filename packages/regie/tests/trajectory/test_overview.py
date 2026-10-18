from __future__ import annotations

from regie.trajectory.rich.state import ParticipantTrajectoryState
from regie.trajectory.rich.view import TrajectoryView
from textual.app import App, ComposeResult
from textual.widgets import Label

from theater.frontend.trajectory import (
    PanelState,
    PanelStateInfo,
    TrajectoryCapabilities,
    TrajectoryCurrentOperation,
    TrajectoryDelta,
    TrajectoryFeature,
    TrajectoryOverview,
    TrajectoryPage,
)


def panel(
    state: PanelState = PanelState.READY, participant_state: str = "live", message: str = ""
) -> PanelStateInfo:
    return PanelStateInfo(state, message, participant_state=participant_state)


def current(
    *,
    start: float | None = None,
    duration_ms: float | None = 8_400,
    summary: str = "pytest tests/test_rpc.py",
) -> TrajectoryCurrentOperation:
    return TrajectoryCurrentOperation(
        record_id="r1",
        kind="tool_call",
        lane="tools",
        status="running",
        summary=summary,
        model="gpt-5.6",
        start=start,
        duration_ms=duration_ms,
    )


def text(label: Label) -> str:
    return str(label.render())


class Host(App):
    def compose(self) -> ComposeResult:
        yield TrajectoryView("p1", id="trajectory", focus_on_mount=False)


def test_state_snapshot_older_and_follow_copy_backend_overview_facts() -> None:
    state = ParticipantTrajectoryState("p1")
    first_capabilities = TrajectoryCapabilities(supported=frozenset({TrajectoryFeature.TOOLS}))
    first_overview = TrajectoryOverview(record_count=2)
    state.apply_snapshot(
        TrajectoryPage(
            panel_state=panel(),
            stream_id="stream",
            capabilities=first_capabilities,
            overview=first_overview,
        )
    )
    assert state.capabilities is first_capabilities
    assert state.overview is first_overview

    older_capabilities = TrajectoryCapabilities(unsupported=frozenset({TrajectoryFeature.USAGE}))
    older_overview = TrajectoryOverview(record_count=4)
    state.apply_older(
        TrajectoryPage(
            panel_state=panel(), capabilities=older_capabilities, overview=older_overview
        )
    )
    assert state.capabilities is older_capabilities
    assert state.overview is older_overview

    follow_capabilities = TrajectoryCapabilities(observed=frozenset({TrajectoryFeature.TIMING}))
    follow_overview = TrajectoryOverview(record_count=5)
    state.apply_follow(
        TrajectoryDelta(
            stream_id="stream", capabilities=follow_capabilities, overview=follow_overview
        )
    )
    assert state.capabilities is follow_capabilities
    assert state.overview is follow_overview


def test_legacy_follow_preserves_backend_overview_facts() -> None:
    capabilities = TrajectoryCapabilities(supported=frozenset({TrajectoryFeature.MODELS}))
    overview = TrajectoryOverview(record_count=3)
    state = ParticipantTrajectoryState(
        "p1", stream_id="stream", capabilities=capabilities, overview=overview
    )

    state.apply_follow(TrajectoryDelta(stream_id="stream"))

    assert state.capabilities is capabilities
    assert state.overview is overview
