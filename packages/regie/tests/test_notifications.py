from __future__ import annotations

from dataclasses import replace
from types import MappingProxyType

from regie.notifications import newly_awaiting_input

from theater.frontend import EventCursor, Participant, StateProjection


def _projection(**statuses: str) -> StateProjection:
    return StateProjection(
        cursor=EventCursor("stream-a", 1),
        participants=MappingProxyType(
            {
                participant_id: Participant.from_wire(
                    {
                        "participant_id": participant_id,
                        "origin": "spawned",
                        "harness": "codex",
                        "status": status,
                        "owner": {"kind": "local_operator", "revision": 1},
                        "name": participant_id,
                        "addressable": False,
                        "presence": "unknown",
                        "actions": {},
                    }
                )
                for participant_id, status in statuses.items()
            }
        ),
        operations=MappingProxyType({}),
        jobs=MappingProxyType({}),
        providers=MappingProxyType({}),
        workspaces=MappingProxyType({}),
    )


def test_input_notifications_include_initial_and_new_entries_and_rearm_after_leaving():
    initial = _projection(first="awaiting_input", second="working")
    assert newly_awaiting_input(None, initial) == ("first",)
    assert newly_awaiting_input(initial, initial) == ()

    changed = _projection(first="working", second="awaiting_input", third="awaiting_input")
    assert newly_awaiting_input(initial, changed) == ("second", "third")
    reentered = _projection(first="awaiting_input", second="awaiting_input")
    assert newly_awaiting_input(changed, reentered) == ("first",)
    assert newly_awaiting_input(reentered, _projection()) == ()


def test_stale_projections_and_metadata_changes_do_not_repeat_notifications():
    initial = _projection(first="awaiting_input")
    assert newly_awaiting_input(None, replace(initial, stale=True)) == ()
    renamed = replace(
        initial,
        participants=MappingProxyType(
            {"first": replace(initial.participants["first"], name="new")}
        ),
    )
    assert newly_awaiting_input(initial, renamed) == ()
