"""The OpenCode TUI live source: status mapping, identity checks and lineage staging."""

from __future__ import annotations

from tests.support.opencode_frontend_events import message_event, status_snapshot
from theater.harness.builtin.plugins.opencode.live import OpenCodeTuiLiveSource
from theater.harness.contracts.runtime import (
    NativeTurnTerminal,
    RuntimeNotification,
)
from theater.models import Status


def _scope(session_id: str = "ses-1", epoch: int = 1) -> dict[str, object]:
    return {
        "session_id": session_id,
        "route_session_id": session_id,
        "session_epoch": epoch,
    }


async def test_live_source_rejects_message_and_part_payloads() -> None:
    source = OpenCodeTuiLiveSource(lambda: "ses-1")
    source.feed(
        RuntimeNotification(
            method="history",
            params={
                **_scope(),
                "messages": [
                    {
                        "info": {
                            "id": "msg-1",
                            "sessionID": "ses-1",
                            "role": "assistant",
                        },
                        "parts": [{"id": "tool-1", "sessionID": "ses-1", "type": "tool"}],
                    }
                ],
            },
        )
    )
    source.feed(
        RuntimeNotification(
            method="event",
            params={
                **_scope(),
                "event": {
                    "id": "event-1",
                    "type": "message.part.updated",
                    "properties": {
                        "part": {
                            "id": "part-1",
                            "sessionID": "ses-1",
                            "messageID": "msg-1",
                            "type": "text",
                            "text": "one",
                        }
                    },
                },
            },
        )
    )
    batch = await source.read()
    assert batch.trajectory == ()
    assert batch.status is None
    assert batch.terminal_evidence == ()
    assert batch.progressed is False


async def test_live_source_maps_public_tui_status_without_completion() -> None:
    source = OpenCodeTuiLiveSource(lambda: "ses-1")
    source.feed(
        RuntimeNotification(
            method="snapshot",
            params={**_scope(), "status": {"type": "busy"}},
        )
    )
    batch = await source.read()
    assert batch.status is Status.WORKING
    assert batch.trajectory == ()
    assert batch.terminal_evidence == ()

    source.feed(
        RuntimeNotification(
            method="snapshot",
            params={**_scope(), "status": {"type": "busy"}},
        )
    )
    duplicate = await source.read()
    assert duplicate.status is Status.WORKING
    assert duplicate.progressed is False

    source.feed(
        RuntimeNotification(
            method="event",
            params={
                **_scope(),
                "event": {
                    "id": "status-1",
                    "type": "session.status",
                    "properties": {"sessionID": "ses-1", "status": {"type": "idle"}},
                },
            },
        )
    )
    batch = await source.read()
    assert batch.status is Status.IDLE
    assert batch.trajectory == ()

    source.feed(
        RuntimeNotification(
            method="snapshot",
            params={**_scope(), "status": {"type": "busy"}},
        )
    )
    assert (await source.read()).status is Status.WORKING

    source.feed(
        RuntimeNotification(
            method="event",
            params={
                **_scope(),
                "event": {
                    "id": "subagent-status-1",
                    "type": "session.status",
                    "properties": {"sessionID": "ses-child", "status": {"type": "busy"}},
                },
            },
        )
    )
    assert (await source.read()).status is Status.WORKING


async def test_live_source_rejects_foreign_sessions_and_stale_epochs() -> None:
    current = {"session_id": "ses-1"}
    source = OpenCodeTuiLiveSource(lambda: current["session_id"])
    source.feed(
        RuntimeNotification(
            method="event",
            params={
                **_scope(),
                "event": {
                    "id": "child-status",
                    "type": "session.status",
                    "properties": {"sessionID": "ses-child", "status": {"type": "busy"}},
                },
            },
        )
    )
    source.feed(
        RuntimeNotification(
            method="snapshot",
            params={**_scope("ses-1", 2), "status": {"type": "busy"}},
        )
    )
    source.feed(
        RuntimeNotification(
            method="snapshot",
            params={**_scope("ses-1", 1), "status": {"type": "idle"}},
        )
    )
    batch = await source.read()
    assert batch.status is Status.WORKING
    assert batch.trajectory == ()

    current["session_id"] = "ses-new"
    source.feed(
        RuntimeNotification(
            method="snapshot",
            params={**_scope("ses-new", 3), "status": {"type": "idle"}},
        )
    )
    batch = await source.read()
    assert batch.status is Status.IDLE
    assert batch.trajectory == ()


async def test_live_source_uses_latest_status_and_discards_it_when_identity_changes() -> None:
    current = {"session_id": "ses-a"}
    source = OpenCodeTuiLiveSource(lambda: current["session_id"])
    source.feed(
        RuntimeNotification(
            method="snapshot",
            params={**_scope("ses-a"), "status": {"type": "busy"}},
        )
    )
    source.feed(
        RuntimeNotification(
            method="snapshot",
            params={**_scope("ses-a"), "status": {"type": "idle"}},
        )
    )
    assert (await source.read()).status is Status.IDLE

    current["session_id"] = "ses-b"
    assert (await source.read()).status is None
    source.feed(
        RuntimeNotification(
            method="snapshot",
            params={**_scope("ses-b", 2), "status": {"type": "idle"}},
        )
    )
    batch = await source.read()
    assert batch.status is Status.IDLE
    assert batch.trajectory == ()


async def test_live_source_rechecks_identity_before_returning_status() -> None:
    trusted = ["ses-1"]
    source = OpenCodeTuiLiveSource(lambda: trusted[0])
    source.feed(
        RuntimeNotification(
            method="snapshot",
            params={**_scope("ses-1"), "status": {"type": "busy"}},
        )
    )
    batch = await source.read()
    assert batch.status is Status.WORKING
    trusted[0] = "ses-2"
    batch = source.validate_enrichment_batch(batch)
    assert batch.status is None
    assert batch.progressed is False


async def test_live_source_stages_interrupted_and_failed_lineage_only_for_submitted_turns() -> None:
    source = OpenCodeTuiLiveSource(lambda: "ses-1")
    source.feed(status_snapshot())
    assert (await source.read()).status is Status.IDLE

    assert source.note_submitted_turn("ses-1", 1, "msg_user_1") is True
    source.feed(
        message_event(
            {
                "id": "msg_asst_1",
                "role": "assistant",
                "sessionID": "ses-1",
                "parentID": "msg_user_1",
                "error": {"name": "MessageAbortedError"},
            }
        )
    )
    batch = await source.read()
    (outcome,) = batch.terminal_evidence
    assert outcome.native_turn_id == "msg_user_1"
    assert outcome.terminal is NativeTurnTerminal.INTERRUPTED
    assert outcome.error_code == "MessageAbortedError"
    source.terminal_evidence_delivered()

    # A failure without the abort marker is FAILED, with the error name kept.
    assert source.note_submitted_turn("ses-1", 1, "msg_user_2") is True
    source.feed(
        message_event(
            {
                "id": "msg_asst_2",
                "role": "assistant",
                "sessionID": "ses-1",
                "parentID": "msg_user_2",
                "error": {"name": "ProviderError"},
            }
        )
    )
    batch = await source.read()
    (outcome,) = batch.terminal_evidence
    assert outcome.native_turn_id == "msg_user_2"
    assert outcome.terminal is NativeTurnTerminal.FAILED
    assert outcome.error_code == "ProviderError"
    source.terminal_evidence_delivered()


async def test_live_source_ignores_lineage_without_a_submitted_turn() -> None:
    source = OpenCodeTuiLiveSource(lambda: "ses-1")
    source.feed(status_snapshot())
    assert (await source.read()).status is Status.IDLE

    source.feed(
        message_event(
            {
                "id": "msg_asst_unknown",
                "role": "assistant",
                "sessionID": "ses-1",
                "parentID": "msg_never_submitted",
                "time": {"completed": 1500},
            }
        )
    )
    # A foreign-session event is out of scope entirely.
    source.feed(
        message_event(
            {
                "id": "msg_asst_foreign",
                "role": "assistant",
                "sessionID": "ses-other",
                "parentID": "msg_user_1",
                "time": {"completed": 1500},
            },
            session_id="ses-1",
        )
    )
    batch = await source.read()
    assert batch.terminal_evidence == ()

    # Once submitted, the same turn's terminal evidence is staged once only.
    assert source.note_submitted_turn("ses-1", 1, "msg_user_1") is True
    source.feed(
        message_event(
            {
                "id": "msg_asst_1",
                "role": "assistant",
                "sessionID": "ses-1",
                "parentID": "msg_user_1",
                "time": {"completed": 1500},
            }
        )
    )
    source.feed(
        message_event(
            {
                "id": "msg_asst_1",
                "role": "assistant",
                "sessionID": "ses-1",
                "parentID": "msg_user_1",
                "time": {"completed": 1600},
            }
        )
    )
    batch = await source.read()
    (outcome,) = batch.terminal_evidence
    assert outcome.native_turn_id == "msg_user_1"
    assert outcome.completed_at == 1.5


async def test_live_source_drops_unsubmitted_turns_when_the_scope_moves() -> None:
    source = OpenCodeTuiLiveSource(lambda: "ses-1")
    source.feed(status_snapshot())
    assert (await source.read()).status is Status.IDLE

    assert source.note_submitted_turn("ses-1", 1, "msg_user_1") is True
    # The route moves before the turn's lineage arrives.
    source.feed(status_snapshot(epoch=2))
    assert source.note_submitted_turn("ses-1", 2, "msg_user_2") is True
    assert source.active_turn_id() == "msg_user_2"

    # The earlier epoch's lineage must not complete the newer turn.
    source.feed(
        message_event(
            {
                "id": "msg_asst_1",
                "role": "assistant",
                "sessionID": "ses-1",
                "parentID": "msg_user_1",
                "time": {"completed": 1500},
            },
            epoch=1,
        )
    )
    batch = await source.read()
    assert batch.terminal_evidence == ()
    assert source.active_turn_id() == "msg_user_2"
