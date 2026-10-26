"""The bus pollers and the serialisation of palette and inspection reads per SDK lane."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from regie.widgets import ParticipantTree

from packages.regie.tests.support.app_fakes import (
    build_app,
)
from theater.frontend import (
    TranscriptCandidate,
)


@pytest.mark.asyncio
async def test_malformed_bus_pages_are_contained_by_both_pollers(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    app, _client, _presentation = build_app()
    messages: list[str] = []
    caplog.set_level("DEBUG", logger="regie")

    async with app.run_test() as pilot:
        await pilot.pause()
        app._bus_visible = True
        monkeypatch.setattr(app, "notify", lambda message, **_kwargs: messages.append(str(message)))

        async def malformed(*_args: object, **_kwargs: object) -> object:
            raise TypeError("malformed diagnostic page")

        monkeypatch.setattr(app._bus, "poll", malformed)
        monkeypatch.setattr(app._animation_bus, "poll", malformed)
        await app._refresh_bus()
        await app._refresh_animations()

    assert messages == []
    assert "diagnostic bus unavailable: malformed diagnostic page" in caplog.text


@pytest.mark.asyncio
async def test_hidden_bus_has_an_independent_route_and_await_animation_cursor() -> None:
    app, client, _presentation = build_app()

    async with app.run_test() as pilot:
        await pilot.pause()
        assert app._animation_primed
        client.diagnostics.rows.append(
            {
                "id": 1,
                "kind": "agent.send",
                "from_id": "participant-1",
                "to_id": "participant-2",
                "payload": {"handle": "send:1"},
            }
        )
        await app._refresh_animations()
        assert len(app._animation.route_anims) == 1
        assert app._animation_bus.after_id == 1
        assert app._bus.after_id == 0

        while app._animation.route_anims:
            app._tick_route_animations()
        client.diagnostics.rows.append(
            {
                "id": 2,
                "kind": "job.await.start",
                "from_id": "participant-1",
                "to_id": "participant-2",
                "payload": {"handle": "send:1", "token": "await:1"},
            }
        )
        await app._refresh_animations()
        assert len(app._animation.await_anims) == 1
        app._tick_route_animations()
        tree = app.query_one(ParticipantTree)
        assert tree._overlaid

        client.diagnostics.rows.append(
            {
                "id": 3,
                "kind": "job.await.end",
                "from_id": "participant-1",
                "to_id": "participant-2",
                "payload": {"handle": "send:1", "token": "await:1"},
            }
        )
        await app._refresh_animations()
        assert app._animation.await_anims == {}
        assert tree._overlaid == set()


@pytest.mark.asyncio
async def test_repeated_palette_and_inspection_reads_serialize_each_sdk_lane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, client, _presentation = build_app()
    active = dict.fromkeys(("catalog", "controls", "resume", "transcripts"), 0)
    maximum = dict(active)

    async def observed(name: str, value: object) -> object:
        active[name] += 1
        maximum[name] = max(maximum[name], active[name])
        await asyncio.sleep(0.01)
        active[name] -= 1
        return value

    entry = (await client.catalogs.harnesses()).value.items[0]
    candidate = TranscriptCandidate(
        "/tmp/candidate.jsonl",
        session_id="candidate-session",
        provenance="exact",
    )

    async def catalog() -> object:
        return await observed("catalog", SimpleNamespace(value=SimpleNamespace(items=(entry,))))

    async def controls(_participant_id: str) -> object:
        return await observed(
            "controls",
            SimpleNamespace(value=SimpleNamespace(extra={}, actions={})),
        )

    async def resume_candidates(**_params: object) -> object:
        return await observed(
            "resume",
            SimpleNamespace(value=SimpleNamespace(items=(), next_cursor=None)),
        )

    async def transcript_candidates(_participant_id: str) -> object:
        return await observed(
            "transcripts",
            SimpleNamespace(value=SimpleNamespace(items=(candidate,))),
        )

    monkeypatch.setattr(client.catalogs, "harnesses", catalog)
    monkeypatch.setattr(client.controls, "get", controls)
    monkeypatch.setattr(client.participants, "resume_candidates", resume_candidates)
    monkeypatch.setattr(client.transcripts, "candidates", transcript_candidates)

    async with app.run_test() as pilot:
        await pilot.pause()
        await asyncio.gather(
            app._load_catalog(),
            app._load_catalog(),
            app._show_controls("participant-1"),
            app._show_controls("participant-2"),
            app.load_resume_sessions(),
            app.load_resume_sessions(),
            app.load_transcript_candidates("participant-1"),
            app.load_transcript_candidates("participant-1"),
        )

    assert maximum == dict.fromkeys(maximum, 1)


async def test_bus_loop_follows_panel_visibility() -> None:
    app, _client, _presentation = build_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app._animation_loop.running and not app._bus_loop.running
        await pilot.press("v")
        assert app._bus_loop.running
        await pilot.press("v")
        assert not app._bus_loop.running
