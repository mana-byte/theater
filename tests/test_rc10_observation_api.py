"""Focused RC10 public observation adapters through the real socket dispatcher."""

from __future__ import annotations

import asyncio
import json
import threading
from types import MappingProxyType, SimpleNamespace

import pytest
from test_transcript_paging import StaticSource
from test_transcript_paging import page as transcript_page

from theater import paths, protocol
from theater.constants.daemon import (
    BUS_KIND_OPERATOR_TRANSCRIPT_BIND,
    BUS_KIND_OPERATOR_TRANSCRIPT_UNBIND,
)
from theater.daemon.frontend import observation_handlers as observation_mod
from theater.daemon.frontend import participant_read_handlers as participant_mod
from theater.daemon.frontend import router as router_mod
from theater.daemon.frontend.handlers import PUBLIC_HANDLERS
from theater.daemon.frontend.job_handlers import JOB_HANDLERS
from theater.daemon.frontend.participant_read_handlers import PARTICIPANT_READ_HANDLERS
from theater.daemon.presence.contracts import PresenceSnapshot, PresenceState
from theater.frontend.capabilities import PUBLIC_API_MAJOR, PUBLIC_API_MINOR
from theater.frontend.dto.transcripts import TranscriptReadPage
from theater.frontend.schemas import catalog as schema_catalog
from theater.harness import HARNESSES
from theater.harness.contracts.events import Event, EventKind, TurnTerminal
from theater.harness.contracts.source import TranscriptCandidate
from theater.models import JobState, Status
from theater.transcript_identity import canonical_location


def _request(request_id: int, method: str, params: dict | None = None, **extra: object) -> bytes:
    return protocol.encode({"id": request_id, "method": method, "params": params or {}, **extra})


def _handshake() -> bytes:
    return _request(
        1,
        "frontend.handshake",
        {
            "api": {"major": PUBLIC_API_MAJOR, "minor": PUBLIC_API_MINOR},
            "client_id": "observation-api-test",
            "role": "operator",
            "channel": "rpc",
            "required_capabilities": [],
        },
    )


async def _exchange(frames: list[bytes]) -> list[dict]:
    reader, writer = await asyncio.open_unix_connection(
        str(paths.socket_path()), limit=max(protocol.MAX_MESSAGE_BYTES, 1024)
    )
    try:
        responses: list[dict] = []
        for frame in frames:
            writer.write(frame)
            await writer.drain()
            responses.append(json.loads(await protocol.read_message(reader)))
        return responses
    finally:
        writer.close()
        await writer.wait_closed()


@pytest.fixture
def observation_public_handlers(monkeypatch: pytest.MonkeyPatch) -> None:
    handlers = {
        **PUBLIC_HANDLERS,
        **PARTICIPANT_READ_HANDLERS,
        **JOB_HANDLERS,
        **observation_mod.OBSERVATION_HANDLERS,
    }
    monkeypatch.setattr(router_mod, "PUBLIC_HANDLERS", MappingProxyType(handlers))


class _Presence:
    def __init__(self, state: PresenceState) -> None:
        self.state = state
        self.revision = 1

    def snapshot(self, _participant_id: str) -> PresenceSnapshot:
        return PresenceSnapshot(self.state, "fixture", self.revision, 0.0)

    async def refresh(self) -> None:
        return None

    async def require_absent(self, _participant_id: str) -> None:
        if self.state is not PresenceState.ABSENT:
            raise AssertionError("fixture expected an absent participant")

    async def wait_for_change(self, _after_revision: int) -> int:
        await asyncio.sleep(3600)
        return self.revision


class _TranscriptObserver:
    location = "/tmp/rc10-observation-fixture.jsonl"

    def __init__(self) -> None:
        self.bind_cwds: list[str | None] = []

    def transcript_candidates(self, *, cwd, domain=None, after=None) -> list[TranscriptCandidate]:
        del cwd, domain, after
        return [
            TranscriptCandidate(
                self.location,
                session_id="fixture-session",
                provenance="exact",
            )
        ]

    def admit_operator_candidate(
        self, *, cwd, candidate: str, domain=None, after=None
    ) -> TranscriptCandidate:
        del domain, after
        self.bind_cwds.append(cwd)
        if candidate != self.location:
            raise ValueError("unknown fixture candidate")
        return TranscriptCandidate(candidate, session_id="fixture-session", provenance="exact")


async def test_public_transcripts_preserve_outcomes_and_project_before_budgeting(
    daemon, observation_public_handlers, monkeypatch
) -> None:
    events = tuple(
        Event(
            kind=EventKind.ASSISTANT,
            text=f"event {index}: " + "é" * 450,
            raw_index=index,
            turn_end=terminal is not None,
            turn_terminal=terminal,
        )
        for index, terminal in enumerate((None, *TurnTerminal))
    )
    source = StaticSource({None: transcript_page(events, cursor="fixed")})
    observer = SimpleNamespace(open_source=lambda **_kwargs: source)
    monkeypatch.setitem(HARNESSES, "fixture", SimpleNamespace(observer=observer))
    participant = daemon.registry.register(harness="fixture", pane=None, cwd="/tmp/public-read")
    cursor = None
    chunks = {index: [] for index in range(len(events))}
    for _ in range(20):
        params = {"participant_id": participant.id, "max_bytes": 2000}
        if cursor is not None:
            params["cursor"] = cursor
        responses = await _exchange(
            [_handshake(), _request(2, "frontend.transcripts.read", params)]
        )
        assert responses[1]["ok"] is True, responses[1]
        wire = responses[1]["result"]
        assert len(json.dumps(wire, ensure_ascii=False, separators=(",", ":")).encode()) <= 2000
        decoded = TranscriptReadPage.from_wire(wire)
        for event in decoded.events:
            expected = events[event.index]
            assert event.turn_terminal is (expected.turn_terminal is not None)
            assert event.turn_outcome == expected.turn_terminal
            chunks[event.index].append((event.text_start_byte, event.text))
        cursor = decoded.next_cursor
        if cursor is None:
            break
    assert cursor is None
    assert ["".join(text for _, text in sorted(chunks[index])) for index in chunks] == [
        event.text for event in events
    ]
    private = await observation_mod.read_transcript_page(daemon, participant_id=participant.id)
    assert [event["turn_terminal"] for event in private["events"]] == [None, *TurnTerminal]
    assert all("turn_outcome" not in event for event in private["events"])


def test_recall_chunks_are_utf8_safe_and_fit_without_linear_reserialization(monkeypatch):
    from theater.daemon.frontend import recall_chunks
    from theater.daemon.frontend.validation import PublicRequestError

    value = {"text": 'é🙂"\\\n' * 2000}
    encoded = recall_chunks._encoded_json(value)
    original = recall_chunks._encoded_json
    calls = 0

    def counted(value):
        nonlocal calls
        calls += 1
        return original(value)

    monkeypatch.setattr(recall_chunks, "_encoded_json", counted)
    offset = 0
    contents = []
    while True:
        calls = 0
        chunk = recall_chunks.recall_chunk("segment", value, offset=offset, max_bytes=4096)
        assert calls <= 16
        assert len(original(chunk)) <= 4096
        contents.append(chunk["content"])
        if chunk["next_offset"] is None:
            break
        assert chunk["next_offset"] > offset
        offset = chunk["next_offset"]
    assert "".join(contents).encode() == encoded
    with pytest.raises(PublicRequestError, match="splits a UTF-8"):
        recall_chunks.recall_chunk(
            "segment", value, offset=encoded.index(b"\xc3") + 1, max_bytes=4096
        )
    with pytest.raises(PublicRequestError, match="increase max_bytes"):
        recall_chunks.recall_chunk("segment", value, offset=0, max_bytes=1)


async def test_participant_reads_page_history_and_missing_ids(
    daemon, observation_public_handlers
) -> None:
    root = daemon.registry.register(harness="vibe", pane=None, cwd="/tmp/root")
    child = daemon.registry.create_spawned(harness="vibe", cwd="/tmp/child", parent_id=root.id)
    daemon.registry.set_status(child.id, Status.DEAD)

    responses = await _exchange(
        [
            _handshake(),
            _request(2, "frontend.participants.list", {"limit": 1}),
            _request(3, "frontend.participants.list", {"limit": 1, "cursor": root.id}),
            _request(4, "frontend.participants.tree", {"participant_id": root.id}),
            _request(5, "frontend.participants.get", {"participant_id": "missing-participant"}),
            _request(6, "frontend.participants.list", {"cursor": "missing-participant"}),
        ]
    )

    assert responses[1]["result"]["items"][0]["participant_id"] == root.id
    assert responses[1]["result"]["next_cursor"] == root.id
    assert responses[1]["result"]["items"][0]["addressable"] is False
    assert responses[1]["result"]["items"][0]["actions"]["send"]["route_available"] is False
    assert responses[2]["result"]["items"][0]["participant_id"] == child.id
    tree = responses[3]["result"]
    assert tree["root_id"] == root.id
    assert [item["participant_id"] for item in tree["items"]] == [root.id, child.id]
    assert tree["items"][1]["status"] == "dead"
    assert responses[4]["error"]["code"] == "not_found"
    assert responses[5]["error"]["code"] == "bad_request"


def test_route_availability_distinguishes_provider_native_and_unbound_paths() -> None:
    participant = SimpleNamespace(status=Status.IDLE, tmux_pane="%legacy", addressable=True)
    terminal_route = {
        "identity": {"provider_id": "provider-a", "provider_generation": 3},
        "health": "healthy",
    }
    provider = SimpleNamespace(
        is_provider=True,
        is_native=False,
        is_legacy=False,
        route_available=True,
        terminal=SimpleNamespace(provider_id="provider-a", provider_generation=3),
    )

    assert participant_mod._physical_route_available(provider, participant, terminal_route, None)

    provider.route_available = False
    assert not participant_mod._physical_route_available(
        provider, participant, terminal_route, None
    )

    provider.route_available = True
    stale_terminal = {
        "identity": {"provider_id": "provider-a", "provider_generation": 4},
        "health": "healthy",
    }
    assert not participant_mod._physical_route_available(
        provider, participant, stale_terminal, None
    )

    native = SimpleNamespace(is_provider=False, is_native=True, is_legacy=False)
    assert participant_mod._physical_route_available(
        native, participant, None, {"health": "connected"}
    )
    assert participant_mod._physical_route_available(
        native, participant, None, {"health": "degraded"}
    )
    assert not participant_mod._physical_route_available(
        native, participant, None, {"health": "disconnected"}
    )

    unbound = SimpleNamespace(is_provider=False, is_native=False, is_legacy=False)
    assert not participant_mod._physical_route_available(
        unbound,
        SimpleNamespace(status=Status.IDLE, tmux_pane=None, addressable=False),
        None,
        None,
    )


async def test_job_reads_keep_structured_results_and_shared_presence_waits(
    daemon, observation_public_handlers
) -> None:
    target = daemon.registry.register(harness="vibe", pane=None, cwd="/tmp/job")
    job = daemon.jobs.create(
        handle="job-observation",
        caller_id="cli",
        target_id=target.id,
        kind="send",
        response_format='{"type":"object"}',
    )
    daemon.jobs.finish(
        job.handle,
        state=JobState.DONE,
        result='{"answer":"done"}',
        raw_result='{"answer":"done"}',
    )
    presence = _Presence(PresenceState.PRESENT)
    daemon.presence = presence

    responses = await _exchange(
        [
            _handshake(),
            _request(2, "frontend.jobs.list", {"participant_id": target.id}),
            _request(3, "frontend.jobs.await", {"job_handles": [job.handle], "wait_seconds": 0}),
        ]
    )

    item = responses[1]["result"]["items"][0]
    assert item["result"] == {"answer": "done"}
    assert item["raw_result"] == '{"answer":"done"}'
    assert responses[2]["result"]["timed_out"] is True
    assert responses[2]["result"]["jobs"][0]["await_reason"] == "timeout"

    presence.state = PresenceState.ABSENT
    released = await _exchange(
        [
            _handshake(),
            _request(2, "frontend.jobs.await", {"job_handles": [job.handle], "wait_seconds": 0}),
            _request(3, "frontend.jobs.get", {"job_handle": "missing-job"}),
            _request(4, "frontend.jobs.await", {"job_handles": ["missing-job"], "wait_seconds": 0}),
        ]
    )

    assert released[1]["result"]["timed_out"] is False
    assert released[1]["result"]["jobs"][0]["await_reason"] == "job_terminal"
    assert released[2]["error"]["code"] == "not_found"
    assert released[3]["error"]["code"] == "not_found"


async def test_transcript_binding_keeps_conflicts_and_cursor_bounds(
    daemon, observation_public_handlers, monkeypatch: pytest.MonkeyPatch
) -> None:
    observer = _TranscriptObserver()
    monkeypatch.setitem(HARNESSES, "fixture", SimpleNamespace(observer=observer))
    daemon.presence = _Presence(PresenceState.ABSENT)
    owner = daemon.registry.register(harness="fixture", pane=None, cwd="/tmp/owner")
    other = daemon.registry.register(harness="fixture", pane=None, cwd="/tmp/other")

    responses = await _exchange(
        [
            _handshake(),
            _request(2, "frontend.transcripts.candidates", {"participant_id": owner.id}),
            _request(
                3,
                "frontend.transcripts.bind",
                {"participant_id": owner.id, "location": observer.location},
                idempotency_key="bind-owner",
            ),
            _request(
                4,
                "frontend.transcripts.bind",
                {"participant_id": owner.id, "location": observer.location},
                idempotency_key="bind-owner",
            ),
            _request(
                5,
                "frontend.transcripts.bind",
                {"participant_id": other.id, "location": observer.location},
                idempotency_key="bind-other",
            ),
            _request(
                6,
                "frontend.transcripts.read",
                {"participant_id": owner.id, "cursor": "x" * 4097},
            ),
        ]
    )

    assert responses[1]["result"]["items"][0]["location"] == canonical_location(observer.location)
    assert responses[2]["result"]["participant_id"] == owner.id
    assert responses[3]["result"] == responses[2]["result"]
    assert observer.bind_cwds == ["/tmp/owner", "/tmp/other"]
    assert responses[4]["error"]["code"] == "bad_request"
    assert responses[5]["error"]["code"] == "bad_request"


async def test_transcript_candidates_bound_large_archives_without_hiding_bindable_rows(
    daemon, observation_public_handlers, monkeypatch: pytest.MonkeyPatch
) -> None:
    loop_thread = threading.get_ident()

    class LargeArchiveObserver:
        def transcript_candidates(
            self, *, cwd, domain=None, after=None
        ) -> list[TranscriptCandidate]:
            assert threading.get_ident() != loop_thread
            del cwd, domain, after
            rejected = [
                TranscriptCandidate(
                    f"/tmp/rejected-{index:03d}.jsonl",
                    session_id=f"rejected-{index:03d}",
                    mtime=float(1_000 - index),
                    rejection_reason="cwd mismatch",
                )
                for index in range(500)
            ]
            bindable = [
                TranscriptCandidate(
                    f"/tmp/bindable-{index}.jsonl",
                    session_id=f"bindable-{index}",
                    mtime=float(index),
                    provenance="exact",
                )
                for index in range(2)
            ]
            return [*rejected, *bindable]

    observer = LargeArchiveObserver()
    monkeypatch.setitem(HARNESSES, "large-archive", SimpleNamespace(observer=observer))
    participant = daemon.registry.register(
        harness="large-archive", pane=None, cwd="/tmp/large-archive"
    )
    ownership_reads = 0
    original_list = daemon.registry.list

    def list_participants(*args, **kwargs):
        nonlocal ownership_reads
        if kwargs.get("include_dead"):
            ownership_reads += 1
        return original_list(*args, **kwargs)

    monkeypatch.setattr(daemon.registry, "list", list_participants)

    responses = await _exchange(
        [
            _handshake(),
            _request(
                2,
                "frontend.transcripts.candidates",
                {"participant_id": participant.id},
            ),
        ]
    )

    result = responses[1]["result"]
    assert len(result["items"]) == 500
    assert [item["session_id"] for item in result["items"][:2]] == [
        "bindable-0",
        "bindable-1",
    ]
    assert result["next_cursor"] is None
    assert ownership_reads == 1


async def test_transcript_bind_replay_repairs_observer_after_committed_failure(
    daemon, observation_public_handlers, monkeypatch: pytest.MonkeyPatch
) -> None:
    observer = _TranscriptObserver()
    monkeypatch.setitem(HARNESSES, "fixture", SimpleNamespace(observer=observer))
    presence = _Presence(PresenceState.ABSENT)
    daemon.presence = presence
    prior_owner = daemon.registry.register(harness="fixture", pane=None, cwd="/tmp/prior")
    target = daemon.registry.register(harness="fixture", pane=None, cwd="/tmp/target")
    prior_owner.transcript_location = canonical_location(observer.location)
    prior_owner.session_id = "prior-session"
    prior_owner.session_correlation = "operator"
    daemon.store.upsert_participant(prior_owner)

    reset_calls: list[str] = []
    records: list[tuple[str, str, str | None, str | None]] = []
    fail_once = True

    async def reset(participant_id: str) -> None:
        nonlocal fail_once
        reset_calls.append(participant_id)
        if fail_once:
            fail_once = False
            raise RuntimeError("fixture observer reset failed")

    def record(
        participant_id: str,
        location: str,
        session_id: str | None,
        *,
        prior_owner: str | None = None,
    ) -> None:
        records.append((participant_id, location, session_id, prior_owner))

    monkeypatch.setattr(daemon.observer, "reset_for_operator_bind", reset)
    monkeypatch.setattr(daemon.observer, "record_operator_binding", record)
    params = {
        "participant_id": target.id,
        "location": observer.location,
        "prior_owner_id": prior_owner.id,
    }

    first = await _exchange(
        [
            _handshake(),
            _request(2, "frontend.transcripts.bind", params, idempotency_key="repair-bind"),
        ]
    )

    assert first[1]["error"]["code"] == "internal"
    durable_target = daemon.store.get_participant(target.id)
    durable_owner = daemon.store.get_participant(prior_owner.id)
    assert durable_target is not None
    assert durable_target.transcript_location == canonical_location(observer.location)
    assert durable_target.session_id == "fixture-session"
    assert durable_owner is not None
    assert durable_owner.transcript_location is None

    presence.state = PresenceState.PRESENT
    replay = await _exchange(
        [
            _handshake(),
            _request(2, "frontend.transcripts.bind", params, idempotency_key="repair-bind"),
        ]
    )

    assert replay[1]["result"] == {
        "participant_id": target.id,
        "location": canonical_location(observer.location),
        "session_id": "fixture-session",
        "prior_owner_id": prior_owner.id,
    }
    assert observer.bind_cwds == ["/tmp/target"]
    assert reset_calls == [prior_owner.id, prior_owner.id, target.id]
    assert records == [
        (target.id, canonical_location(observer.location), "fixture-session", prior_owner.id)
    ]
    audit = [
        event["kind"]
        for event in daemon.store.bus_tail(limit=20)
        if event["kind"] in {BUS_KIND_OPERATOR_TRANSCRIPT_UNBIND, BUS_KIND_OPERATOR_TRANSCRIPT_BIND}
    ]
    assert audit == [BUS_KIND_OPERATOR_TRANSCRIPT_UNBIND, BUS_KIND_OPERATOR_TRANSCRIPT_BIND]


async def test_recall_adapters_page_domain_results(
    daemon, observation_public_handlers, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def recall(_store, *, paths: list[str], depth: int) -> dict:
        assert paths == ["src/example.py"]
        assert depth == 500
        return {
            "src/example.py": {
                "current": "abc",
                "timeline": [{"segment": "job-a"}, {"segment": "job-b"}],
            }
        }

    async def read_segment(segment_id: str, **_kwargs: object) -> dict:
        return {"segment": segment_id, "kind": "job", "transcript": {"available": False}}

    monkeypatch.setattr(observation_mod, "_recall_query", recall)
    monkeypatch.setattr("theater.daemon.recall_read.read_segment", read_segment)

    responses = await _exchange(
        [
            _handshake(),
            _request(2, "frontend.recall.query", {"path": "src/example.py", "limit": 1}),
            _request(
                3,
                "frontend.recall.query",
                {"path": "src/example.py", "cursor": "recall1:1", "limit": 1},
            ),
            _request(4, "frontend.recall.read", {"segment_id": "job-a"}),
            _request(5, "frontend.recall.query", {"path": "src/example.py", "cursor": "bad"}),
        ]
    )

    assert responses[1]["result"]["items"] == [{"path": "src/example.py", "segment": "job-a"}]
    assert responses[1]["result"]["next_cursor"] == "recall1:1"
    assert responses[2]["result"]["items"] == [{"path": "src/example.py", "segment": "job-b"}]
    assert responses[3]["result"]["segment"] == "job-a"
    assert responses[4]["error"]["code"] == "bad_request"


async def test_trajectory_streams_keep_cursor_lifecycles_independent(
    daemon, observation_public_handlers
) -> None:
    first = daemon.registry.register(harness="vibe", pane=None, cwd="/tmp/first")
    second = daemon.registry.register(harness="vibe", pane=None, cwd="/tmp/second")

    snapshots = await _exchange(
        [
            _handshake(),
            _request(2, "frontend.trajectory.snapshot", {"participant_id": first.id}),
            _request(3, "frontend.trajectory.snapshot", {"participant_id": second.id}),
        ]
    )
    first_page = snapshots[1]["result"]
    second_page = snapshots[2]["result"]

    responses = await _exchange(
        [
            _handshake(),
            _request(
                2,
                "frontend.trajectory.follow",
                {
                    "stream_id": first_page["stream_id"],
                    "cursor": second_page["cursor"],
                    "wait_seconds": 0,
                },
            ),
            _request(3, "frontend.trajectory.close", {"stream_id": first_page["stream_id"]}),
            _request(
                4,
                "frontend.trajectory.follow",
                {
                    "stream_id": second_page["stream_id"],
                    "cursor": second_page["cursor"],
                    "wait_seconds": 0,
                },
            ),
            _request(
                5,
                "frontend.trajectory.follow",
                {
                    "stream_id": second_page["stream_id"],
                    "cursor": "not-a-cursor",
                    "wait_seconds": 0,
                },
            ),
        ]
    )

    assert responses[1]["result"]["resync_required"] is True
    assert responses[2]["result"]["released"] is True
    assert responses[3]["result"].get("resync_required") is not True
    assert responses[4]["error"]["code"] == "bad_request"


async def test_trajectory_result_is_validated_once_and_invalid_output_is_refused(
    daemon, observation_public_handlers, monkeypatch
) -> None:
    original = schema_catalog.validator_for
    validations = []
    loop_thread = threading.get_ident()

    def counted(schema_id):
        if schema_id.endswith("trajectory_snapshotResult"):
            validations.append(schema_id)
            assert threading.get_ident() != loop_thread
        return original(schema_id)

    monkeypatch.setattr(schema_catalog, "validator_for", counted)
    monkeypatch.setattr(observation_mod, "validator_for", counted)
    participant = daemon.registry.register(harness="vibe", pane=None, cwd="/tmp/validation")
    request = _request(2, "frontend.trajectory.snapshot", {"participant_id": participant.id})
    responses = await _exchange([_handshake(), request])
    assert responses[1]["ok"] is True
    assert len(validations) == 1

    async def invalid(*_args, **_kwargs):
        return SimpleNamespace(to_wire=lambda: {"invalid": {"not", "json"}})

    monkeypatch.setattr(daemon.trajectory, "snapshot", invalid)
    responses = await _exchange([_handshake(), request])
    assert responses[1]["error"]["code"] == "internal"
