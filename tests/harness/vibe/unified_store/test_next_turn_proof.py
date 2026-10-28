"""Vibe unified store: proving a later turn's end from the two views after journal collection."""

from __future__ import annotations

import asyncio
import shutil
from typing import Any

import pytest

from tests.support.vibe_store import (
    A_ENDS,
    ANSWER_A,
    GEN1,
    GEN2,
    RUNNING_A,
    USER_A,
    Store,
    attached_to_running_a,
    make_state,
    public_effect,
    public_message,
    turn_doc,
    turn_record,
)
from theater.harness.builtin.plugins.vibe.observer import VibeObserver
from theater.harness.contracts.events import EventKind

GEN3 = "0" * 15 + "3"

GEN4 = "0" * 15 + "4"

USER_B = public_message("user-b", "user", "job B prompt", turn_id="turn-b")

# An injected turn types no user entry (Vibe skips it), so the entry it can leave is its answer.
ANSWER_C = public_message("asst-c", "assistant", "ANSWER C", turn_id="turn-c")

# B's input entry reusing an id the previous view already acknowledges.
USER_B_REUSE = public_message("user-a", "user", "job B prompt", turn_id="turn-b")

UNTAGGED_EFFECT = {key: value for key, value in public_effect().items() if key != "turnId"}

ANSWER_B = public_message("asst-b", "assistant", "ANSWER B", turn_id="turn-b")

ANSWER_A_ALTERED = public_message("asst-a", "assistant", "ANSWER A CHANGED", turn_id="turn-a")

# A turn whose pre-agent hook is pending: the session turns busy while latestTurn
# still shows the previous turn's end, so an injected turn can already be active.
PRE_AGENT_ACTIVE = turn_doc([USER_A, ANSWER_A], "turn-a", "completed")
PRE_AGENT_ACTIVE["session"]["status"] = {"type": "running"}


def turn_doc_with_input(
    entries: list[dict[str, Any]], turn_id: str, status: str, input_entry_id: str
) -> dict[str, Any]:
    """The shipped builder omits ``inputEntryId``; the real store's latestTurn carries it."""
    state = turn_doc(entries, turn_id, status)
    state["latestTurn"]["inputEntryId"] = input_entry_id
    return state


def ends(batch: Any) -> list[tuple[EventKind, str, str | None, bool]]:
    return [
        (event.kind, event.text, event.turn_id, event.turn_end)
        for event in batch.events
        if not event.usage_only
    ]


def _attach_acknowledged(store: Store, state: dict[str, Any]) -> Any:
    store.publish(generation=GEN1, snapshot_sequence=0, state=state, watermark=1)
    source = VibeObserver(root=store.session_root.parent.parent).open_source(cwd="/tmp/work")
    attached = asyncio.run(source.read())
    assert attached.attached is not None
    source.commit_attachment()
    source.acknowledge_source_checkpoint()
    return source


def _b_folds_away(
    store: Store,
    *,
    base: int,
    settled: dict[str, Any],
    current: dict[str, Any],
    current_watermark: int | None = None,
) -> None:
    """Publish B's whole lifecycle across three folds, then keep only the two newest journals."""
    store.publish(
        generation=GEN2,
        snapshot_sequence=base,
        state=settled,
        watermark=base + 1,
        journal=[turn_record(base + 2, "turn-b", "in_progress", USER_B)],
    )
    store.publish(
        generation=GEN3,
        snapshot_sequence=base + 1,
        state=turn_doc_with_input([USER_A, ANSWER_A, USER_B], "turn-b", "in_progress", "user-b"),
        watermark=base + 2,
        journal=[turn_record(base + 3, "turn-b", "completed", ANSWER_B)],
    )
    store.publish(
        generation=GEN4,
        snapshot_sequence=base + 2,
        state=current,
        watermark=base + 3 if current_watermark is None else current_watermark,
        journal=[("core_input", {})],
    )
    shutil.rmtree(store.generation_dir(GEN1))
    shutil.rmtree(store.generation_dir(GEN2))
    for sequence in range(1, base + 2):
        # One journal file per publish: a folded generation can leave this range sparse.
        (store.session_root / "journal" / f"{sequence:016d}.jsonl").unlink(missing_ok=True)


@pytest.mark.parametrize("extra_publishes", [0, 1])
def test_a_later_turns_end_is_proved_from_the_two_views(store: Store, extra_publishes: int) -> None:
    """Two folds collect the acknowledged journal; the views alone must still prove B's end."""
    source = attached_to_running_a(store)
    store.publish(
        generation=GEN1, snapshot_sequence=0, state=RUNNING_A, watermark=1, journal=[A_ENDS]
    )
    first = asyncio.run(source.read())
    assert [end for end in ends(first) if end[3]] == [
        (EventKind.ASSISTANT, "ANSWER A", "turn-a", True)
    ]
    source.acknowledge_source_checkpoint()

    _b_folds_away(
        store,
        base=1,
        settled=turn_doc([USER_A, ANSWER_A], "turn-a", "completed"),
        current=turn_doc_with_input(
            [USER_A, ANSWER_A, USER_B, ANSWER_B], "turn-b", "completed", "user-b"
        ),
        current_watermark=4 + extra_publishes,
    )

    batch = asyncio.run(source.read())
    assert batch.turn_state is not None and batch.turn_state.complete
    assert ends(batch) == [
        (EventKind.USER, "job B prompt", "turn-b", False),
        (EventKind.ASSISTANT, "ANSWER B", "turn-b", True),
    ]


def _current_state(case: str) -> dict[str, Any]:
    history: list[dict[str, Any]] = [USER_A, ANSWER_A, USER_B, ANSWER_B]
    input_entry_id = "user-b"
    if case == "injected turn's answer entry":
        history = [USER_A, ANSWER_A, ANSWER_C, USER_B, ANSWER_B]
    elif case == "input entry id reused":
        history = [USER_A, ANSWER_A, USER_B_REUSE, ANSWER_B]
        input_entry_id = "user-a"
    elif case == "input entry names the answer":
        input_entry_id = "asst-b"
    elif case == "appended entry without a turnId":
        history = [USER_A, ANSWER_A, UNTAGGED_EFFECT, USER_B, ANSWER_B]
    elif case == "previous entry removed":
        history = [USER_A, USER_B, ANSWER_B]
    elif case == "previous entry altered":
        history = [USER_A, ANSWER_A_ALTERED, USER_B, ANSWER_B]
    elif case == "input entry not new":
        input_entry_id = "user-a"
    if case == "latestTurn missing":
        return make_state(history)
    return turn_doc_with_input(history, "turn-b", "completed", input_entry_id)


@pytest.mark.parametrize(
    "case",
    [
        "previous turn in_progress",
        "pre-agent turn already active",
        "injected turn's answer entry",
        "input entry id reused",
        "input entry names the answer",
        "input entry not new",
        "appended entry without a turnId",
        "too many publishes in the gap",
        "watermark gap too small",
        "previous entry removed",
        "previous entry altered",
        "latestTurn missing",
    ],
)
def test_an_unprovable_gap_fails_closed_and_names_its_facts(
    store: Store, case: str, caplog: pytest.LogCaptureFixture
) -> None:
    """Each hole in the two-view proof keeps the read unproven and logs the decisive facts."""
    if case == "previous turn in_progress":
        source = _attach_acknowledged(store, turn_doc([USER_A, ANSWER_A], "turn-a", "in_progress"))
        base = 0
        settled = turn_doc([USER_A, ANSWER_A], "turn-a", "in_progress")
    elif case == "pre-agent turn already active":
        source = attached_to_running_a(store)
        store.publish(
            generation=GEN1,
            snapshot_sequence=0,
            state=RUNNING_A,
            watermark=1,
            journal=[
                A_ENDS,
                # The pre-agent start publish: busy session, latestTurn still A's end.
                ("projection_advanced", {"watermark": 3, "snapshot": PRE_AGENT_ACTIVE}),
            ],
        )
        asyncio.run(source.read())
        source.acknowledge_source_checkpoint()
        base = 2
        settled = PRE_AGENT_ACTIVE
    else:
        source = attached_to_running_a(store)
        store.publish(
            generation=GEN1, snapshot_sequence=0, state=RUNNING_A, watermark=1, journal=[A_ENDS]
        )
        asyncio.run(source.read())
        source.acknowledge_source_checkpoint()
        base = 1
        settled = turn_doc([USER_A, ANSWER_A], "turn-a", "completed")

    _b_folds_away(
        store,
        base=base,
        settled=settled,
        current=_current_state(case),
        # A consistent gap is exactly B's two publishes; smaller means the count itself
        # is broken, larger leaves room for an invisible injected turn.
        current_watermark={
            "too many publishes in the gap": 7,
            "watermark gap too small": 3,
            # Acknowledged after C's pre-agent start publish: three publishes left.
            "pre-agent turn already active": 6,
        }.get(case),
    )

    batch = asyncio.run(source.read())
    assert batch.turn_state is not None and not batch.turn_state.complete
    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.levelname == "WARNING" and "not replayable" in record.getMessage()
    ]
    assert warnings, "an unprovable gap must log the warning"
    expected_turns = {
        "previous turn in_progress": "turns turn-a/in_progress -> turn-b/completed",
        "latestTurn missing": "turns turn-a/completed -> None/None",
    }.get(case, "turns turn-a/completed -> turn-b/completed")
    assert expected_turns in warnings[0]
    assert f"retained journals [{base + 2}, {base + 3}]" in warnings[0]
