from __future__ import annotations

from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest

from theater.daemon.presence import PresenceSnapshot, PresenceState
from theater.daemon.presence.access import check_absent, presence_snapshot, require_absent
from theater.models import HumanPresent


@pytest.mark.parametrize(
    ("state", "protected"),
    [
        (PresenceState.PRESENT, True),
        (PresenceState.UNKNOWN, True),
        (PresenceState.ABSENT, False),
    ],
)
def test_snapshot_protects_everything_except_confirmed_absence(
    state: PresenceState, protected: bool
) -> None:
    snapshot = PresenceSnapshot(state, "focus", 7, 123.5)

    assert snapshot.protected is protected


def test_snapshot_serializes_the_exact_shared_wire_shape() -> None:
    snapshot = PresenceSnapshot(PresenceState.UNKNOWN, "never observed", 3, None)

    assert snapshot.to_dict() == {
        "state": "unknown",
        "protected": True,
        "reason": "never observed",
        "revision": 3,
        "observed_at": None,
    }


def test_snapshot_is_immutable() -> None:
    snapshot = PresenceSnapshot(PresenceState.ABSENT, "blurred", 2, 123.5)

    with pytest.raises(FrozenInstanceError):
        snapshot.state = PresenceState.PRESENT  # type: ignore[misc]


async def test_missing_provider_never_grants_mutation_or_projects_absence():
    daemon = SimpleNamespace()
    assert presence_snapshot(daemon, "p1").state is PresenceState.UNKNOWN
    with pytest.raises(HumanPresent):
        check_absent(daemon, "p1")
    with pytest.raises(HumanPresent, match="await_sessions"):
        await require_absent(daemon, "p1")


def test_projection_failure_is_unknown_without_extra_io():
    def failed_snapshot(participant_id):
        raise RuntimeError("cache unavailable")

    daemon = SimpleNamespace(presence=SimpleNamespace(snapshot=failed_snapshot))
    result = presence_snapshot(daemon, "p1")
    assert result.state is PresenceState.UNKNOWN
    assert result.protected
