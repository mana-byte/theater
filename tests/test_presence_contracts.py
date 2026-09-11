from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from theater.daemon.presence import PresenceSnapshot, PresenceState


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
