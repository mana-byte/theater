"""The spawn and resume palettes, the harness catalog and the spawned agent's selection."""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from typing import cast

import pytest
from regie.app_parts.projection import followed_spawn
from regie.controllers.actions import ActionRecord, ActionState
from regie.dashboard.widgets import WelcomeDashboard
from regie.state import StateController
from regie.widgets import ParticipantTree
from regie.widgets.name_editor import DirectoryEditor
from textual.command import CommandInput, CommandPalette

from packages.regie.tests.support.app_fakes import (
    FakePresentation,
    FakeState,
    build_app,
    make_participant,
    make_projection,
)
from packages.regie.tests.support.waiting import wait_until
from theater.frontend import FrontendTransportError, StateProjection
from theater.frontend import ResumeCandidate as PublicResumeCandidate
from theater.frontend.dto.catalogs import HarnessCatalogEntry


@pytest.mark.asyncio
async def test_spawn_palette_accepts_a_completed_explicit_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "project with spaces"
    target.mkdir()
    monkeypatch.chdir(tmp_path)
    app, client, _presentation = build_app()

    async with app.run_test() as pilot:
        await pilot.press("ctrl+p")
        palette = app.screen.query_one(CommandInput)
        palette.value = "spawn"
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, CommandPalette)
        palette = app.screen.query_one(CommandInput)
        palette.value = "codex"
        await pilot.press("enter")
        await pilot.pause()
        # The new agent's row is in the tree; its directory is typed where it will show.
        tree = app.query_one(ParticipantTree)
        await wait_until(pilot, lambda: bool(app.query(DirectoryEditor)))
        row = tree._key_widgets[("n", "new")]
        cwd_input = app.query_one(DirectoryEditor)
        assert cwd_input.parent is row and cwd_input.styles.offset.y.value == 2
        assert client.participants.spawned == []
        assert cwd_input.value == str(tmp_path)
        await pilot.press("x")  # the caret sits after the prefilled path, nothing selected
        assert cwd_input.value == f"{tmp_path}x"
        await pilot.press("enter")
        # A missing directory keeps the row and reopens its editor with what was typed.
        await wait_until(pilot, lambda: bool(app.query(DirectoryEditor)))
        cwd_input = app.query_one(DirectoryEditor)
        assert cwd_input.value == f"{tmp_path}x"
        cwd_input.value = "proj"
        await pilot.press("tab")
        assert cwd_input.value == f"project with spaces{os.sep}"
        await pilot.press("enter")
        await wait_until(pilot, lambda: ("n", "new") not in tree._key_widgets)
        await pilot.pause()

    assert client.participants.spawned == [("codex", None, "manual")]
    assert client.participants.spawn_options[0]["cwd"] == str(target)


@pytest.mark.asyncio
async def test_textual_refetches_and_acknowledges_coalesced_catalog_invalidations() -> None:
    app, client, _presentation = build_app()
    dirty = replace(make_projection(), catalog_dirty=True, catalog_generation=7)
    state = FakeState(dirty)
    app._state = cast(StateController, state)

    async with app.run_test() as pilot:
        await pilot.pause()
        client.catalogs.calls = 0
        await app._synchronize_projection()
        await app._synchronize_projection()

    assert client.catalogs.calls == 1
    assert state.catalog_acknowledgements == [7]
    assert state.projection.catalog_dirty is False


@pytest.mark.asyncio
async def test_malformed_remote_catalog_falls_back_to_local_harnesses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, client, _presentation = build_app()
    fallback = HarnessCatalogEntry.from_wire(
        {
            "name": "claude",
            "binary": "claude",
            "binaries": [],
            "icon": "◆",
            "installed": True,
            "compatible": True,
            "supported_wiring": ["tmux"],
            "requires_terminal": True,
            "provider_ready": True,
            "launch_available": True,
            "approvals": ["manual"],
            "reason": None,
            "detail": None,
        }
    )

    async def malformed_catalog() -> object:
        raise TypeError("invalid catalog payload")

    monkeypatch.setattr(client.catalogs, "harnesses", malformed_catalog)
    monkeypatch.setattr("regie.app_parts.startup.local_harness_catalog", lambda: (fallback,))
    monkeypatch.setattr(
        FakePresentation,
        "unmanaged_panes",
        lambda *_args, **_kwargs: _async_value(()),
    )

    async with app.run_test() as pilot:
        await pilot.pause()
        assert app._harnesses == (fallback,)
        assert "claude" in str(app.query_one("#dashboard-harnesses").render())


@pytest.mark.asyncio
async def test_textual_resume_uses_a_bounded_public_dead_session_and_trusted_context() -> None:
    app, client, _presentation = build_app()
    resumable = PublicResumeCandidate.from_wire(
        {
            "participant_id": "dead-resumable",
            "harness": "codex",
            "name": "old session",
            "cwd": "/workspace/original",
            "resume_state": "resumable",
            "transcript_identity": {
                "state": "trusted",
                "session_id": "trusted-session",
                "provenance": "exact",
                "location": "/tmp/transcript-a",
                "domain": None,
                "detail": None,
            },
        }
    )
    unavailable = PublicResumeCandidate.from_wire(
        {
            "participant_id": "dead-untrusted",
            "harness": "codex",
            "name": "old untrusted session",
            "cwd": "/workspace/other",
            "resume_state": "untrusted",
            "transcript_identity": {
                "state": "untrusted",
                "session_id": "untrusted-session",
                "provenance": "heuristic",
                "location": "/tmp/transcript-b",
                "domain": None,
                "detail": "not verified",
            },
        }
    )
    client.participants.dead_rows = (resumable, unavailable)

    async with app.run_test() as pilot:
        await pilot.pause()
        discovery = await app.load_resume_sessions()
        assert client.participants.resume_calls == [{"limit": 20}]
        candidates = {candidate.participant_id: candidate for candidate in discovery.candidates}
        assert candidates["dead-untrusted"].reason == "transcript identity could not be verified"

        app.resume_dead_session(candidates["dead-resumable"])
        await wait_until(pilot, lambda: bool(client.participants.spawned))

        assert client.participants.spawned[0][:2] == ("codex", None)
        assert client.participants.spawn_options[0]["cwd"] == "/workspace/original"
        assert client.participants.spawn_options[0]["resume"] == "trusted-session"


async def _async_value(value: object) -> object:
    return value


@pytest.mark.asyncio
async def test_completed_spawn_is_selected_staged_and_focused(caplog) -> None:
    caplog.set_level("INFO", logger="regie")
    app, _client, presentation = build_app()

    async with app.run_test() as pilot:
        await pilot.pause()
        state = cast(FakeState, app._state)
        spawned = make_participant("participant-3", name="spawned")
        state.projection = replace(
            state.projection,
            participants=MappingProxyType(
                {**state.projection.participants, spawned.participant_id: spawned}
            ),
        )
        refetches = state.initialize_calls + state.catch_up_calls

        await app._reconcile_completed_action(
            ActionRecord(
                "spawn",
                "spawn-target",
                "spawn-key",
                participant_id=spawned.participant_id,
                state=ActionState.SUCCEEDED,
                operation_id="spawn-operation",
            )
        )
        await app.workers.wait_for_complete()
        for _ in range(50):  # staging runs on the presentation queue
            if presentation.focused:
                break
            await pilot.pause()

        assert app.selected_participant_id == spawned.participant_id
        assert ("p", spawned.participant_id) in app.query_one(ParticipantTree)._key_widgets
        assert [target.terminal_id for target in presentation.staged] == ["%3"]
        assert [target.terminal_id for target in presentation.focused] == ["%3"]
        # The follow lane had already delivered the spawn: neither a catch-up nor a snapshot.
        assert state.initialize_calls + state.catch_up_calls == refetches
        phases = [
            row.message
            for row in caplog.records
            if row.name == "regie.latency" and row.message.startswith("action.spawn.")
        ]
        assert [line.split()[0] for line in phases] == [
            "action.spawn.projection",
            "action.spawn.unmanaged",
        ]
        assert all("operation=spawn-operation result=success" in line for line in phases)
        for _ in range(50):  # rendering is acknowledged after the next refresh
            if "after_projection_ms=" in caplog.text:
                break
            await pilot.pause()
        assert "after_projection_ms=" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("catch_up_result", ["delivers", "lags", "fails"])
async def test_spawn_the_follow_lane_missed_is_caught_up_before_any_snapshot(
    catch_up_result: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("INFO", logger="regie")
    app, _client, presentation = build_app()

    async with app.run_test() as pilot:
        await pilot.pause()
        state = cast(FakeState, app._state)
        assert state.projection is not None
        spawned = make_participant("participant-3", name="spawned")
        delivered = replace(
            state.projection,
            participants=MappingProxyType(
                {**state.projection.participants, spawned.participant_id: spawned}
            ),
        )
        refetches = state.initialize_calls

        async def catch_up() -> None:
            state.catch_up_calls += 1
            if catch_up_result == "fails":
                raise FrontendTransportError("lost")
            if catch_up_result == "delivers":
                state.projection = delivered

        async def refetch() -> StateProjection:
            state.initialize_calls += 1
            state.projection = delivered
            return delivered

        monkeypatch.setattr(state, "catch_up", catch_up)
        monkeypatch.setattr(state, "initialize", refetch)
        await app._reconcile_completed_action(
            ActionRecord(
                "spawn",
                "spawn-target",
                "spawn-key",
                participant_id=spawned.participant_id,
                state=ActionState.SUCCEEDED,
                operation_id="spawn-operation",
            )
        )
        await app.workers.wait_for_complete()
        for _ in range(50):  # staging runs on the presentation queue
            if presentation.focused:
                break
            await pilot.pause()

        phases = [
            row.message.split()[0]
            for row in caplog.records
            if row.name == "regie.latency" and row.message.startswith("action.spawn.")
        ]
        assert [target.terminal_id for target in presentation.focused] == ["%3"]
        assert state.catch_up_calls == 1
        assert state.initialize_calls == refetches + (catch_up_result != "delivers")
        assert ("action.spawn.snapshot" in phases) is (catch_up_result != "delivers")


def _routed_spawn(*, routed: bool = True, stale: bool = False) -> StateProjection:
    spawned = make_participant("participant-3", name="spawned")
    projection = make_projection()
    return replace(
        projection,
        participants=MappingProxyType(
            {
                **projection.participants,
                spawned.participant_id: spawned
                if routed
                else replace(spawned, terminal_route=None),
            }
        ),
        stale=stale,
    )


@pytest.mark.parametrize(
    ("projection", "participant_id", "held"),
    [
        (_routed_spawn(), "participant-3", True),
        (_routed_spawn(), "participant-4", False),
        (_routed_spawn(routed=False), "participant-3", False),
        (_routed_spawn(stale=True), "participant-3", False),
        (None, "participant-3", False),
    ],
    ids=["held", "absent", "unrouted", "stale", "no-projection"],
)
def test_followed_spawn_needs_a_current_routed_participant(
    projection: StateProjection | None, participant_id: str, held: bool
) -> None:
    assert followed_spawn(projection, participant_id) is (projection if held else None)


@pytest.mark.asyncio
async def test_harnesses_without_an_executable_are_not_offered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, client, _presentation = build_app()
    installed = (await client.catalogs.harnesses()).value.items[0]
    missing = HarnessCatalogEntry.from_wire(
        {
            **{name: getattr(installed, name) for name in ("supported_wiring", "approvals")},
            "name": "pi",
            "binary": "pi",
            "installed": False,
            "compatible": True,
            "requires_terminal": True,
            "provider_ready": True,
            "launch_available": False,
            "reason": "executable not found",
            "detail": None,
        }
    )

    async def catalog() -> object:
        return SimpleNamespace(value=SimpleNamespace(items=(installed, missing)))

    monkeypatch.setattr(client.catalogs, "harnesses", catalog)
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.wait_for_catalog()

        assert [choice.harness for choice in app._spawn_choices()] == ["codex"]
        assert all(entry.name != "pi" for entry in app._installed_harnesses)
        assert app.icon_for_harness("codex") == "◈"  # the full catalog still names icons
        rows = app.query_one(WelcomeDashboard)._harnesses or []
        assert [row["name"] for row in rows] == ["codex"]
