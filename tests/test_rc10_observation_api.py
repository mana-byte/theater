"""Focused RC10 public observation adapters through the real socket dispatcher."""

from __future__ import annotations

import asyncio
import json
from types import MappingProxyType, SimpleNamespace

import pytest

from theater import paths, protocol
from theater.daemon.frontend import observation_handlers as observation_mod
from theater.daemon.frontend import router as router_mod
from theater.daemon.frontend.handlers import PUBLIC_HANDLERS
from theater.daemon.frontend.job_handlers import JOB_HANDLERS
from theater.daemon.frontend.participant_read_handlers import PARTICIPANT_READ_HANDLERS
from theater.daemon.presence.contracts import PresenceSnapshot, PresenceState
from theater.frontend.capabilities import PUBLIC_API_MAJOR, PUBLIC_API_MINOR
from theater.harness import HARNESSES
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
        ]
    )

    assert responses[1]["result"]["items"][0]["participant_id"] == root.id
    assert responses[1]["result"]["next_cursor"] == root.id
    assert responses[2]["result"]["items"][0]["participant_id"] == child.id
    tree = responses[3]["result"]
    assert tree["root_id"] == root.id
    assert [item["participant_id"] for item in tree["items"]] == [root.id, child.id]
    assert tree["items"][1]["status"] == "dead"
    assert responses[4]["error"]["code"] == "not_found"


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
        ]
    )

    assert released[1]["result"]["timed_out"] is False
    assert released[1]["result"]["jobs"][0]["await_reason"] == "job_terminal"
    assert released[2]["error"]["code"] == "not_found"


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
        ]
    )

    assert responses[1]["result"]["items"] == [{"path": "src/example.py", "segment": "job-a"}]
    assert responses[1]["result"]["next_cursor"] == "recall1:1"
    assert responses[2]["result"]["items"] == [{"path": "src/example.py", "segment": "job-b"}]
    assert responses[3]["result"]["segment"] == "job-a"


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
        ]
    )

    assert responses[1]["result"]["resync_required"] is True
    assert responses[2]["result"]["released"] is True
    assert responses[3]["result"].get("resync_required") is not True
