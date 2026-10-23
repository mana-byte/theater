"""Transcript recovery in the UI: candidates, ownership, retries and independent clients."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import MappingProxyType, SimpleNamespace
from typing import cast

import pytest
from regie.controllers.transcripts import TranscriptBindingController, TranscriptBindState
from regie.state import StateController
from textual.command import CommandInput

from packages.regie.tests.support.app_fakes import (
    FakeClient,
    FakeState,
    build_app,
    make_participant,
    make_projection,
)
from theater.frontend import (
    FrontendClient,
    FrontendTransportError,
    ResponseValidationError,
    TranscriptBindResult,
    TranscriptCandidate,
)


@pytest.mark.asyncio
async def test_rejected_transcript_candidate_is_never_bound() -> None:
    app, client, _presentation = build_app()
    candidate = TranscriptCandidate(
        "/tmp/rejected.jsonl",
        session_id="session-rejected",
        rejection_reason="candidate belongs to another workspace",
    )

    async with app.run_test() as pilot:
        await pilot.pause()
        app._transcript_recovery_target = "participant-1"
        app.select_transcript_candidate(candidate)
        await pilot.pause()

    assert client.transcripts.bind_calls == []


@pytest.mark.asyncio
async def test_transcript_recovery_hides_for_trusted_identity_and_filters_rejections() -> None:
    app, client, _presentation = build_app()
    trusted = make_participant(
        "participant-1",
        name="first",
        transcript_identity={
            "state": "trusted",
            "session_id": "session-a",
            "provenance": "exact",
            "location": "/tmp/trusted.jsonl",
            "domain": None,
            "detail": None,
        },
    )
    state = FakeState(
        replace(
            make_projection(),
            participants=MappingProxyType(
                {
                    trusted.participant_id: trusted,
                    "participant-2": make_participant("participant-2", name="second"),
                }
            ),
        )
    )
    app._state = cast(StateController, state)
    accepted = TranscriptCandidate(
        "/tmp/accepted.jsonl",
        session_id="accepted",
        provenance="exact",
    )
    rejected = TranscriptCandidate(
        "/tmp/rejected.jsonl",
        session_id="rejected",
        rejection_reason="belongs to another participant",
    )
    client.transcripts.candidate_rows = (accepted, rejected)

    async with app.run_test() as pilot:
        await pilot.pause()
        assert not app.transcript_recovery_available("participant-1")
        assert app.transcript_recovery_available("participant-2")
        app._transcript_recovery_target = "participant-2"
        assert await app.load_transcript_candidates() == (accepted,)


@pytest.mark.asyncio
async def test_transcript_recovery_palette_preserves_target_through_selection_and_cancel() -> None:
    app, client, _presentation = build_app()
    candidate = TranscriptCandidate(
        "/tmp/accepted.jsonl",
        session_id="accepted",
        provenance="exact",
    )
    client.transcripts.candidate_rows = (candidate,)

    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_recover_transcript()
        await pilot.pause()
        assert app._transcript_recovery_target == "participant-1"

        palette = app.screen.query_one(CommandInput)
        palette.value = "accepted.jsonl"
        await pilot.press("enter")
        for _ in range(20):
            if client.transcripts.bind_calls:
                break
            await pilot.pause()

        assert client.transcripts.bind_calls[0]["participant_id"] == "participant-1"
        assert app._transcript_recovery_target is None
        assert app.focused is None

        app.action_recover_transcript()
        await pilot.pause()
        assert app._transcript_recovery_target == "participant-1"
        await pilot.press("escape")
        await pilot.pause()
        assert app._transcript_recovery_target is None
        assert app.focused is None


@pytest.mark.asyncio
@pytest.mark.parametrize("owner_field", ["owner_id", "tombstone_id"])
async def test_an_owned_transcript_is_never_transferred_from_the_ui(owner_field: str) -> None:
    app, client, _presentation = build_app()
    candidate = TranscriptCandidate(
        "/tmp/transfer.jsonl", session_id="session-transfer", **{owner_field: "prior-owner"}
    )
    notes: list[str] = []
    async with app.run_test() as pilot:
        await pilot.pause()
        app.notify = lambda message, **_kwargs: notes.append(str(message))  # type: ignore[method-assign]
        app._transcript_recovery_target = "participant-1"
        app.select_transcript_candidate(candidate)
        await pilot.pause()
        assert app.screen is app.screen_stack[0]
    assert client.transcripts.bind_calls == []
    assert any("--transfer-from prior-owner" in note for note in notes)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "first_error",
    [
        FrontendTransportError("connection dropped after submission"),
        ResponseValidationError("invalid bind response"),
    ],
)
async def test_uncertain_transcript_bind_retries_original_parameters_and_key(
    first_error: Exception,
) -> None:
    client = FakeClient()
    bind_client = FakeClient()
    bind_client.transcripts.bind_outcomes = [
        first_error,
        SimpleNamespace(
            value=TranscriptBindResult(
                "participant-1",
                "/tmp/original.jsonl",
                prior_owner_id="prior-owner",
            )
        ),
    ]
    controller = TranscriptBindingController(
        cast(FrontendClient, client),
        client_factory=lambda: cast(FrontendClient, bind_client),
    )

    first = await controller.bind(
        "participant-1",
        "/tmp/original.jsonl",
        prior_owner_id="prior-owner",
    )
    assert first.state is TranscriptBindState.UNCERTAIN
    original_key = first.idempotency_key
    second = await controller.bind(
        "participant-1",
        "/tmp/original.jsonl",
        prior_owner_id="changed-owner",
    )

    assert first is second
    assert first.state is TranscriptBindState.SUCCEEDED
    assert [call["prior_owner_id"] for call in bind_client.transcripts.bind_calls] == [
        "prior-owner",
        "prior-owner",
    ]
    assert {call["idempotency_key"] for call in bind_client.transcripts.bind_calls} == {
        original_key
    }
    assert bind_client.closed


@pytest.mark.asyncio
async def test_concurrent_transcript_binds_receive_independent_clients() -> None:
    root = FakeClient()
    created: list[FakeClient] = []

    def factory() -> FrontendClient:
        client = FakeClient()
        created.append(client)
        return cast(FrontendClient, client)

    controller = TranscriptBindingController(
        cast(FrontendClient, root),
        client_factory=factory,
    )
    first, second = await asyncio.gather(
        controller.bind("participant-1", "/tmp/one.jsonl", prior_owner_id=None),
        controller.bind("participant-2", "/tmp/two.jsonl", prior_owner_id=None),
    )

    assert first.state is TranscriptBindState.SUCCEEDED
    assert second.state is TranscriptBindState.SUCCEEDED
    assert len(created) == 2
    assert created[0] is not created[1]
    assert [client.closed for client in created] == [True, True]


@pytest.mark.asyncio
async def test_transcript_bind_snapshot_failure_stops_before_unmanaged_refresh(monkeypatch) -> None:
    app, client, _presentation = build_app()
    refreshed = False

    async def fail_snapshot():
        raise FrontendTransportError("snapshot offline")

    async def refresh_unmanaged(*_args, **_kwargs):
        nonlocal refreshed
        refreshed = True

    async with app.run_test() as pilot:
        await pilot.pause()
        assert app._startup_task is not None
        await app._startup_task
        monkeypatch.setattr(app._state, "initialize", fail_snapshot)
        monkeypatch.setattr(app, "_refresh_unmanaged", refresh_unmanaged)
        await app._bind_transcript_candidate("participant-1", "/tmp/accepted.jsonl")

    assert client.transcripts.bind_calls[0]["location"] == "/tmp/accepted.jsonl"
    assert app._last_state_error == ("stale", "snapshot offline")
    assert not refreshed
