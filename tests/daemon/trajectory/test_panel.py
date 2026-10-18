from __future__ import annotations

import pytest

from theater.daemon.trajectory.history import HistoryLoad
from theater.daemon.trajectory.panel import initial_panel_state, participant_state
from theater.harness.contracts.events import Event, EventKind
from theater.harness.contracts.source import HistoryPage
from theater.models import Participant, Status, Tier
from theater.trajectory import PanelState, PanelStateInfo, TrajectoryParticipantState
from theater.transcript_identity import TRANSCRIPT_IDENTITY_LOST_CODE


def _participant(*, status: Status = Status.IDLE, tier: Tier = Tier.ADOPTED) -> Participant:
    return Participant(id="p", harness="fake", status=status, tier=tier)


@pytest.mark.parametrize(
    ("status", "tier", "expected"),
    [
        (Status.IDLE, Tier.ADOPTED, TrajectoryParticipantState.LIVE),
        (Status.DEAD, Tier.ADOPTED, TrajectoryParticipantState.DEAD),
        (Status.IDLE, Tier.EXTERNAL, TrajectoryParticipantState.EXTERNAL),
    ],
)
def test_participant_state_is_pure(
    status: Status, tier: Tier, expected: TrajectoryParticipantState
) -> None:
    assert participant_state(_participant(status=status, tier=tier)) is expected


def test_initial_panel_is_ready_for_trusted_history() -> None:
    result = HistoryLoad(
        HistoryPage(location="/tmp/p", events=(Event(kind=EventKind.ASSISTANT, text="ok"),)),
        source_epoch="epoch",
        trusted=True,
        message="loaded",
    )

    panel = initial_panel_state(
        _participant(),
        current_participant_state=TrajectoryParticipantState.LIVE,
        result=result,
        has_transcript=False,
        live_allowed=False,
    )

    assert panel == PanelStateInfo(PanelState.READY, "loaded", TrajectoryParticipantState.LIVE)


def test_initial_panel_waits_for_an_empty_trusted_history() -> None:
    result = HistoryLoad(HistoryPage(), source_epoch="epoch", trusted=True)

    panel = initial_panel_state(
        _participant(),
        current_participant_state=TrajectoryParticipantState.LIVE,
        result=result,
        has_transcript=False,
        live_allowed=False,
    )

    assert panel == PanelStateInfo(
        PanelState.WAITING,
        "the transcript source is waiting for its first record",
        TrajectoryParticipantState.LIVE,
    )


@pytest.mark.parametrize(
    ("status", "expected"),
    [(Status.IDLE, PanelState.UNTRUSTED), (Status.DEAD, PanelState.UNAVAILABLE)],
)
def test_initial_panel_handles_lost_identity_by_participant_liveness(
    status: Status, expected: PanelState
) -> None:
    result = HistoryLoad(
        HistoryPage(error_code=TRANSCRIPT_IDENTITY_LOST_CODE),
        source_epoch=None,
        trusted=False,
        message="rebind transcript",
    )

    panel = initial_panel_state(
        _participant(status=status),
        current_participant_state=TrajectoryParticipantState.LIVE,
        result=result,
        has_transcript=False,
        live_allowed=False,
    )

    assert panel == PanelStateInfo(expected, "rebind transcript", TrajectoryParticipantState.LIVE)


def test_initial_panel_rejects_untrusted_provenance() -> None:
    result = HistoryLoad(
        HistoryPage(provenance="heuristic"),
        source_epoch=None,
        trusted=False,
    )

    panel = initial_panel_state(
        _participant(),
        current_participant_state=TrajectoryParticipantState.LIVE,
        result=result,
        has_transcript=False,
        live_allowed=False,
    )

    assert panel == PanelStateInfo(
        PanelState.UNTRUSTED,
        "transcript identity is not trusted",
        TrajectoryParticipantState.LIVE,
    )


def test_initial_panel_rejects_ambiguous_history() -> None:
    result = HistoryLoad(
        HistoryPage(provenance="operator"),
        source_epoch=None,
        trusted=False,
        ambiguous=True,
        message="bind the session",
    )

    panel = initial_panel_state(
        _participant(),
        current_participant_state=TrajectoryParticipantState.LIVE,
        result=result,
        has_transcript=False,
        live_allowed=False,
    )

    assert panel == PanelStateInfo(
        PanelState.UNTRUSTED,
        "bind the session",
        TrajectoryParticipantState.LIVE,
    )


def test_initial_panel_retains_cached_transcript_records_after_failure() -> None:
    result = HistoryLoad(
        HistoryPage(error_code="source_failed", error="reader closed"),
        source_epoch=None,
        trusted=False,
        message="reader closed",
    )

    panel = initial_panel_state(
        _participant(),
        current_participant_state=TrajectoryParticipantState.LIVE,
        result=result,
        has_transcript=True,
        live_allowed=False,
    )

    assert panel == PanelStateInfo(
        PanelState.STALE,
        "transcript history is unavailable; cached records remain (reader closed)",
        TrajectoryParticipantState.LIVE,
    )


def test_initial_panel_is_unavailable_without_history_or_cache() -> None:
    result = HistoryLoad(
        HistoryPage(error_code="source_failed"),
        source_epoch=None,
        trusted=False,
    )

    panel = initial_panel_state(
        _participant(),
        current_participant_state=TrajectoryParticipantState.LIVE,
        result=result,
        has_transcript=False,
        live_allowed=False,
    )

    assert panel == PanelStateInfo(
        PanelState.UNAVAILABLE,
        "trajectory history is unavailable",
        TrajectoryParticipantState.LIVE,
    )
