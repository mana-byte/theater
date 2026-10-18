"""Fake daemon client, state and presentation for driving RegieApp in UI tests."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Mapping
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from typing import cast

from regie.app import RegieApp
from regie.contracts import PresentationTarget, RegieSettings, UnmanagedPane
from regie.state import StateController

from theater.frontend import (
    AcceptedOperation,
    EventCursor,
    FrontendClient,
    FrontendResponseError,
    Participant,
    Provider,
    StateProjection,
    TranscriptBindResult,
    TranscriptCandidate,
)
from theater.frontend import ResumeCandidate as PublicResumeCandidate
from theater.frontend.dto.catalogs import HarnessCatalogEntry


def capability() -> dict[str, object]:
    return {"supported": True, "route_available": True, "admissible": True}


def make_participant(
    participant_id: str,
    *,
    name: str,
    status: str = "idle",
    cwd: str | None = None,
    description: str | None = None,
    parent_id: str | None = None,
    trusted_identity: dict[str, str] | None = None,
    transcript_identity: dict[str, str | None] | None = None,
) -> Participant:
    return Participant.from_wire(
        {
            "participant_id": participant_id,
            "origin": "spawned",
            "harness": "codex",
            "status": status,
            "owner": {"kind": "local_operator", "revision": 1},
            "name": name,
            "cwd": cwd,
            "description": description,
            "parent_id": parent_id,
            "addressable": True,
            "presence": "absent",
            "actions": {
                "send": capability(),
                "steer": capability(),
                "queue_followup": capability(),
                "interrupt": capability(),
                "settings_update": capability(),
            },
            "terminal_route": {
                "identity": {
                    "provider_id": "provider-a",
                    "provider_generation": 1,
                    "terminal_id": f"%{participant_id[-1]}",
                    "terminal_incarnation": f"incarnation-{participant_id}",
                    "occupant": {"harness": "codex"},
                    "process": None,
                },
                "health": "healthy",
            },
            "trusted_identity": trusted_identity,
            "transcript_identity": transcript_identity,
        }
    )


def make_projection() -> StateProjection:
    first = make_participant("participant-1", name="first")
    second = make_participant("participant-2", name="second")
    provider = Provider.from_wire(
        {
            "provider_id": "provider-a",
            "selector": "tmux",
            "kind": "tmux",
            "generation": 1,
            "health": "healthy",
            "capabilities": [],
        }
    )
    return StateProjection(
        cursor=EventCursor("stream-a", 1),
        participants=MappingProxyType({first.participant_id: first, second.participant_id: second}),
        operations=MappingProxyType({}),
        jobs=MappingProxyType({}),
        providers=MappingProxyType({provider.provider_id: provider}),
        workspaces=MappingProxyType({}),
    )


class FakeState:
    def __init__(self, projection: StateProjection) -> None:
        self.projection: StateProjection | None = projection
        self.initialize_calls = 0
        self.catalog_acknowledgements: list[int] = []

    async def initialize(self) -> StateProjection:
        self.initialize_calls += 1
        assert self.projection is not None
        return self.projection

    async def synchronize(self) -> StateProjection:
        assert self.projection is not None
        return self.projection

    async def follow(self) -> StateProjection:
        await asyncio.Future()

    def acknowledge_catalogs(self, generation: int) -> bool:
        self.catalog_acknowledgements.append(generation)
        assert self.projection is not None
        if not self.projection.catalog_dirty or generation != self.projection.catalog_generation:
            return False
        self.projection = replace(self.projection, catalog_dirty=False)
        return True


class _Catalogs:
    def __init__(self) -> None:
        self.calls = 0

    async def harnesses(self) -> object:
        self.calls += 1
        entry = HarnessCatalogEntry.from_wire(
            {
                "name": "codex",
                "binary": "codex",
                "icon": "◈",
                "installed": True,
                "compatible": True,
                "supported_wiring": ["tmux"],
                "requires_terminal": True,
                "provider_ready": True,
                "launch_available": True,
                "approvals": ["manual", "edits", "yolo"],
                "reason": None,
                "detail": None,
            }
        )
        return SimpleNamespace(value=SimpleNamespace(items=(entry,)))


class _Usage:
    def __init__(self) -> None:
        self.by_harness_calls: list[dict[str, object]] = []
        self.by_participant_calls: list[dict[str, object]] = []
        self.participant_cost_microcents = 42_000_000

    async def totals(self, *, since: float) -> object:
        del since
        return SimpleNamespace(value={"tokens": 1})

    async def summary(self, *, since: float) -> object:
        del since
        return SimpleNamespace(value={})

    async def by_harness(
        self,
        *,
        since: float | None,
        detailed: bool = False,
    ) -> object:
        self.by_harness_calls.append({"since": since, "detailed": detailed})
        period = {
            "input_tokens": 12,
            "output_tokens": 8,
            "reasoning_output_tokens": 2,
            "cache_read_input_tokens": 4,
            "cache_creation_input_tokens": 1,
            "cost_microcents": 50_000_000,
            "active_days": 1,
        }
        row: dict[str, object] = {
            "harness": "codex",
            "today": period,
            "week": period,
            "month": period,
        }
        if detailed:
            row["models"] = [{"model": "fixture-model", **row}]
            value = {
                "harnesses": [row],
                "totals": {"today": period, "week": period, "month": period},
            }
        else:
            value = {"harnesses": [row]}
        return SimpleNamespace(value=value)

    async def by_participant(
        self,
        *,
        since: float | None,
        participant_ids: tuple[str, ...] | None = None,
        limit: int,
    ) -> object:
        self.by_participant_calls.append(
            {"since": since, "participant_ids": participant_ids, "limit": limit}
        )
        return SimpleNamespace(
            value={
                "since": since,
                "truncated": False,
                "participants": [
                    {
                        "participant_id": "participant-1",
                        "harness": "codex",
                        "models": ["fixture-model"],
                        "input_tokens": 12,
                        "output_tokens": 8,
                        "cache_creation_input_tokens": 0,
                        "cache_read_input_tokens": 0,
                        "reasoning_output_tokens": 0,
                        "cost_microcents": self.participant_cost_microcents,
                        "first_at": 1.0,
                        "last_at": 2.0,
                    }
                ],
            }
        )


class _Diagnostics:
    def __init__(self) -> None:
        self.rows: list[dict[str, object]] = []
        self.calls: list[dict[str, int]] = []

    async def bus_tail(self, *, after_id: int, limit: int, wait_seconds: float = 0) -> object:
        self.calls.append({"after_id": after_id, "limit": limit})
        items = tuple(row for row in self.rows if int(row["id"]) > after_id)[:limit]
        if wait_seconds and not items:
            await asyncio.sleep(0.1)  # stands in for the daemon-side block; 100 Hz kept the CPU hot
        return SimpleNamespace(value=SimpleNamespace(items=items))


class _Controls:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str, str | None]] = []

    async def send(self, participant_id: str, prompt: str, *, idempotency_key: str) -> object:
        self.requests.append(("send", participant_id, prompt))
        return accepted(participant_id)

    async def steer(self, participant_id: str, prompt: str, *, idempotency_key: str) -> object:
        self.requests.append(("steer", participant_id, prompt))
        return accepted(participant_id)

    async def queue_followup(
        self,
        participant_id: str,
        prompt: str,
        *,
        idempotency_key: str,
    ) -> object:
        self.requests.append(("queue_followup", participant_id, prompt))
        return accepted(participant_id)

    async def interrupt(self, participant_id: str, *, idempotency_key: str) -> object:
        self.requests.append(("interrupt", participant_id, None))
        return accepted(participant_id)

    async def get(self, participant_id: str) -> object:
        return SimpleNamespace(value={"participant_id": participant_id})


class _Participants:
    def __init__(self) -> None:
        self.terminated: list[str] = []
        self.spawned: list[tuple[str, str | None, str]] = []
        self.spawn_options: list[dict[str, object]] = []
        self.resume_calls: list[dict[str, object]] = []
        self.dead_rows: tuple[PublicResumeCandidate, ...] = ()
        self.renames: list[dict[str, object]] = []
        self.rename_error: FrontendResponseError | None = None

    async def terminate(self, participant_id: str, *, idempotency_key: str) -> object:
        self.terminated.append(participant_id)
        return accepted(participant_id)

    async def update(
        self,
        participant_id: str,
        *,
        idempotency_key: str,
        name: object = None,
        description: object = None,
    ) -> object:
        del description
        self.renames.append(
            {"participant_id": participant_id, "name": name, "idempotency_key": idempotency_key}
        )
        if self.rename_error is not None:
            raise self.rename_error
        return SimpleNamespace(value=SimpleNamespace(participant_id=participant_id))

    async def spawn(
        self,
        harness: str,
        prompt: str | None,
        approval: str,
        *,
        idempotency_key: str,
        cwd: str | None = None,
        resume: str | None = None,
    ) -> object:
        self.spawned.append((harness, prompt, approval))
        self.spawn_options.append(
            {"cwd": cwd, "resume": resume, "idempotency_key": idempotency_key}
        )
        return accepted("participant-spawned")

    async def resume_candidates(self, **params: object) -> object:
        self.resume_calls.append(params)
        return SimpleNamespace(value=SimpleNamespace(items=self.dead_rows, next_cursor=None))


class _Transcripts:
    def __init__(self) -> None:
        self.candidate_rows: tuple[TranscriptCandidate, ...] = ()
        self.candidate_calls: list[str] = []
        self.bind_calls: list[dict[str, object]] = []
        self.bind_outcomes: list[object] = []

    async def candidates(self, participant_id: str) -> object:
        self.candidate_calls.append(participant_id)
        return SimpleNamespace(value=SimpleNamespace(items=self.candidate_rows))

    async def bind(
        self,
        participant_id: str,
        location: str,
        *,
        idempotency_key: str,
        prior_owner_id: str | None = None,
    ) -> object:
        self.bind_calls.append(
            {
                "participant_id": participant_id,
                "location": location,
                "prior_owner_id": prior_owner_id,
                "idempotency_key": idempotency_key,
            }
        )
        if self.bind_outcomes:
            outcome = self.bind_outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome
        return SimpleNamespace(
            value=TranscriptBindResult(
                participant_id,
                location,
                prior_owner_id=prior_owner_id,
            )
        )


class _Trajectory:
    def __init__(self) -> None:
        self.closed: list[str] = []
        self.snapshot_limits: list[int] = []

    async def snapshot(
        self,
        participant_id: str,
        *,
        limit: int,
        before: str | None = None,
    ) -> object:
        del before
        self.snapshot_limits.append(limit)
        return SimpleNamespace(
            value={
                "panel_state": {"state": "ready", "participant_state": "live"},
                "stream_id": "trajectory-a",
                "cursor": "cursor-a",
                "older_cursor": None,
                "has_older": False,
                "records": [
                    {
                        "record_id": "record-a",
                        "participant_id": participant_id,
                        "revision": 1,
                        "source_epoch": "epoch-a",
                        "lane": "model",
                        "kind": "assistant",
                        "source": "claude",
                        "summary": "public trajectory record",
                        "status": "completed",
                        "turn_id": "turn-a",
                    },
                    {
                        "record_id": "record-b",
                        "participant_id": participant_id,
                        "revision": 2,
                        "source_epoch": "epoch-a",
                        "lane": "model",
                        "kind": "assistant",
                        "source": "claude",
                        "summary": "second public trajectory record",
                        "status": "completed",
                        "turn_id": "turn-b",
                    },
                ],
                "groups": [],
            }
        )

    async def follow(self, stream_id: str, cursor: str, *, wait_seconds: int) -> object:
        del stream_id, cursor
        await asyncio.sleep(wait_seconds)  # the daemon holds the long-poll open; never spin hot
        return SimpleNamespace(value={"stream_id": "trajectory-a", "upserts": []})

    async def search(self, participant_id: str, query: str, *, limit: int) -> object:
        del participant_id, query, limit
        return SimpleNamespace(value=SimpleNamespace(items=()))

    async def close(self, stream_id: str) -> object:
        self.closed.append(stream_id)
        return SimpleNamespace(value={"released": True})


class FakeClient:
    def __init__(self) -> None:
        self.catalogs = _Catalogs()
        self.usage = _Usage()
        self.controls = _Controls()
        self.participants = _Participants()
        self.transcripts = _Transcripts()
        self.trajectory = _Trajectory()
        self.diagnostics = _Diagnostics()
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class FakePresentation:
    def __init__(self) -> None:
        self.staged: list[PresentationTarget] = []
        self.focused: list[PresentationTarget] = []
        self.target_window_calls = 0
        self.unmanaged: tuple[UnmanagedPane, ...] = ()
        self.copied: list[str] = []
        self.terminals_exist = True

    async def open(self) -> None:
        return None

    async def close(self) -> None:
        return None

    def can_stage(self, target: PresentationTarget) -> tuple[bool, str | None]:
        return True, None

    def retain(self, terminals: Iterable[str]) -> None:
        del terminals

    async def target_window(self) -> str:
        self.target_window_calls += 1
        return "@regie"

    async def terminal_exists(self, target: PresentationTarget) -> bool:
        return self.terminals_exist

    async def stage_terminal(self, target: PresentationTarget, *, target_window: str) -> None:
        assert target_window == "@regie"
        self.staged.append(target)

    async def unstage_terminal(self, target: PresentationTarget) -> None:
        self.staged.remove(target)

    async def focus_terminal(self, target: PresentationTarget) -> None:
        self.focused.append(target)

    async def resize_regie(self, *, width: int) -> None:
        del width

    async def resize_pane(
        self,
        pane_id: str,
        *,
        width: int | None = None,
        height: int | None = None,
    ) -> None:
        del pane_id, width, height

    async def copy_text(self, text: str) -> None:
        self.copied.append(text)

    async def unmanaged_panes(
        self, *, harness_commands: Mapping[str, tuple[str, ...]]
    ) -> tuple[UnmanagedPane, ...]:
        assert "codex" in harness_commands
        return self.unmanaged


def accepted(participant_id: str) -> object:
    return SimpleNamespace(
        value=AcceptedOperation(
            operation_id=f"operation-{participant_id}",
            state="succeeded",
            participant_id=participant_id,
        )
    )


def build_app(
    *,
    usage_visible: bool = True,
    tree_layout_path: Path | None = None,
    projection: StateProjection | None = None,
) -> tuple[RegieApp, FakeClient, FakePresentation]:
    """Most UI tests exercise the footer, so they opt into showing it."""
    client = FakeClient()
    presentation = FakePresentation()
    app = RegieApp(
        client=cast(FrontendClient, client),
        settings=RegieSettings(
            tree_interval=60,
            bus_interval=60,
            startup_reveal=False,
            usage_visible=usage_visible,
        ),
        presentation=presentation,
        tree_layout_path=tree_layout_path,
    )
    app._state = cast(StateController, FakeState(projection or make_projection()))
    return app, client, presentation
