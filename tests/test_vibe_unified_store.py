"""Focused compatibility tests for Vibe's Unified Session Store."""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

import pytest
import rfc8785

from theater.harness.builtin.plugins.vibe import unified_store
from theater.harness.builtin.plugins.vibe.manifest import _vibe_stream_floor
from theater.harness.builtin.plugins.vibe.observer import VibeObserver
from theater.harness.builtin.plugins.vibe.unified_store import (
    UnifiedStoreError,
    UnifiedStoreRequiresNewer,
    load_unified_store,
)
from theater.harness.contracts.callbacks import StreamFloorContext
from theater.harness.contracts.events import EventKind
from theater.provenance import TranscriptProvenance
from theater.trajectory.enums import TrajectoryKind, TrajectoryStatus

SESSION_ID = "sess-1"
GEN1 = "0" * 15 + "1"
GEN2 = "0" * 15 + "2"
GEN3 = "0" * 15 + "3"


def canonical(value: Any) -> bytes:
    return rfc8785.dumps(value)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_hex(canonical(value))


def basic_entry(entry_id: str, text: str | None = None) -> dict[str, Any]:
    return {"id": entry_id, "text": text or entry_id}


def make_state(
    entries: list[dict[str, Any]],
    *,
    status: str = "idle",
    session_id: str = SESSION_ID,
) -> dict[str, Any]:
    return {
        "format": "harness.public-session-state/v1",
        "session": {
            "id": session_id,
            "status": {"type": status},
            "createdAt": 1_000,
            "updatedAt": 2_000,
        },
        "history": {
            "range": "latest",
            "entries": entries,
            "cursor": {"before": None, "after": None},
        },
        "turnQueue": {"items": [], "paused": False, "maxItems": 8},
        "activeCallbacks": [],
        "latestTurn": None,
    }


def make_runtime(
    snapshot_sequence: int,
    *,
    session_id: str,
    parent_session_id: str | None,
) -> dict[str, Any]:
    return {
        "runtime_state_version": 3,
        "session_id": session_id,
        "snapshot_sequence": snapshot_sequence,
        "session_metadata": {
            "root_session_id": parent_session_id or session_id,
            "parent_session_id": parent_session_id,
            "cwd": "/tmp/work",
        },
        "children": [],
    }


def projection_delta(watermark: int, *ops: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    return "projection_delta", {"watermark": watermark, "delta": list(ops)}


class Store:
    """Small writer for the subset of store-format v1 used by these tests."""

    def __init__(self, root: Path, session_id: str = SESSION_ID) -> None:
        self.session_root = root / "unified" / session_id
        self.session_root.mkdir(parents=True)
        self.session_id = session_id

    @property
    def current(self) -> Path:
        return self.session_root / "CURRENT"

    def generation_dir(self, generation: str) -> Path:
        return self.session_root / "generations" / generation

    def publish(
        self,
        *,
        generation: str,
        snapshot_sequence: int,
        state: dict[str, Any],
        watermark: int,
        journal: list[tuple[str, dict[str, Any]]] | None = None,
        pooled: bool = False,
        store_minor: int | None = 4,
        parent_session_id: str | None = None,
    ) -> None:
        first_sequence = snapshot_sequence + 1
        checkpoint = {"checkpoint_version": 1, "context": {"messages": state["history"]["entries"]}}
        runtime = make_runtime(
            snapshot_sequence,
            session_id=self.session_id,
            parent_session_id=parent_session_id,
        )
        projection = {
            "projection_state_version": 1,
            "session_id": self.session_id,
            "snapshot_sequence": snapshot_sequence,
            "watermark": watermark,
            "snapshot": state,
        }
        checkpoint_chunks = self._pool(checkpoint, ("context", "messages")) if pooled else None
        projection_chunks = (
            self._pool(projection, ("snapshot", "history", "entries")) if pooled else None
        )

        generation_dir = self.generation_dir(generation)
        generation_dir.mkdir(parents=True, exist_ok=True)
        self._write(generation_dir / "checkpoint.json", checkpoint)
        self._write(generation_dir / "runtime-state.json", runtime)
        self._write(generation_dir / "projection-state.json", projection)
        records = self._journal_records(first_sequence, journal or [])
        journal_path = self.session_root / "journal" / f"{first_sequence:016d}.jsonl"
        journal_path.parent.mkdir(parents=True, exist_ok=True)
        journal_path.write_bytes(b"".join(canonical(record) + b"\n" for record in records))

        manifest = {
            "manifest_version": 1,
            "session_id": self.session_id,
            "generation": generation,
            "created_at": "2026-09-10T10:00:00.000Z",
            "snapshot_sequence": snapshot_sequence,
            "execution_state": "quiescent",
            "checkpoint": {
                "path": "checkpoint.json",
                "sha256": sha256_json(checkpoint),
                "chunks": checkpoint_chunks,
                "checkpoint_version": 1,
            },
            "runtime_state": {
                "path": "runtime-state.json",
                "sha256": sha256_json(runtime),
                "chunks": None,
            },
            "projection_state": {
                "path": "projection-state.json",
                "sha256": sha256_json(projection),
                "chunks": projection_chunks,
            },
            "interop_export": None,
            "recovery_journal_segment": {
                "path": f"journal/{first_sequence:016d}.jsonl",
                "first_sequence": first_sequence,
            },
        }
        manifest_body = canonical(manifest)
        (generation_dir / "manifest.json").write_bytes(manifest_body + b"\n")
        pointer: dict[str, Any] = {
            "store_format": unified_store.STORE_FORMAT,
            "session_id": self.session_id,
            "generation": generation,
            "snapshot_sequence": snapshot_sequence,
            "manifest_sha256": sha256_hex(manifest_body),
        }
        if store_minor is not None:
            pointer["store_format_minor"] = store_minor
        self._write(self.current, pointer)

    def _journal_records(
        self, first_sequence: int, journal: list[tuple[str, dict[str, Any]]]
    ) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        previous: str | None = None
        for offset, (record_type, payload) in enumerate(journal):
            record = {
                "recovery_journal_record_version": 1,
                "sequence": first_sequence + offset,
                "previous_record_sha256": previous,
                "type": record_type,
                "payload": payload,
            }
            record["record_sha256"] = sha256_json(record)
            records.append(record)
            previous = record["record_sha256"]
        return records

    def _pool(self, document: dict[str, Any], path: tuple[str, ...]) -> list[str]:
        node: Any = document
        for key in path[:-1]:
            node = node[key]
        body = canonical(node[path[-1]])
        digest = sha256_hex(body)
        chunk = self.session_root / "chunks" / f"{digest}.json"
        chunk.parent.mkdir(parents=True, exist_ok=True)
        chunk.write_bytes(body + b"\n")
        node[path[-1]] = []
        return [digest]

    @staticmethod
    def _write(path: Path, value: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(canonical(value) + b"\n")


@pytest.fixture()
def store(tmp_path: Path) -> Store:
    return Store(tmp_path)


def publish_default(store: Store, **overrides: Any) -> None:
    values: dict[str, Any] = {
        "generation": GEN1,
        "snapshot_sequence": 0,
        "state": make_state([basic_entry("entry-0")]),
        "watermark": 1,
    }
    values.update(overrides)
    store.publish(**values)


def read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_bytes())
    assert isinstance(value, dict)
    return value


@pytest.mark.parametrize("minor", [None, 1, 2, 3, 4])
def test_reader_supports_every_known_store_minor(tmp_path: Path, minor: int | None) -> None:
    store = Store(tmp_path / str(minor))
    publish_default(store, store_minor=minor)
    view = load_unified_store(store.current)
    assert view is not None
    assert view.store_minor == (1 if minor is None else minor)


def test_reader_reassembles_chunks_and_replays_projection_records(store: Store) -> None:
    appended = basic_entry("entry-1")
    publish_default(
        store,
        pooled=True,
        journal=[
            ("core_input", {"input_id": "input-1"}),
            projection_delta(2, {"op": "append_entry", "entry": appended}),
        ],
    )

    view = load_unified_store(store.current)
    assert view is not None
    assert view.sequence == 2
    assert view.watermark == 2
    assert [entry["id"] for entry in view.snapshot["history"]["entries"]] == [
        "entry-0",
        "entry-1",
    ]


def test_projection_delta_mutations_follow_entry_identity(store: Store) -> None:
    publish_default(
        store,
        state=make_state([basic_entry("keep"), basic_entry("remove")]),
        journal=[
            projection_delta(
                2,
                {"op": "replace_entry", "id": "keep", "entry": basic_entry("keep", "new")},
                {"op": "remove_entry", "id": "remove"},
                {"op": "append_entry", "entry": basic_entry("added")},
            )
        ],
    )

    view = load_unified_store(store.current)
    assert view is not None
    assert view.snapshot["history"]["entries"] == [
        basic_entry("keep", "new"),
        basic_entry("added"),
    ]


def test_historical_read_finds_retained_generation_and_prefix(store: Store) -> None:
    store.publish(
        generation=GEN1,
        snapshot_sequence=0,
        state=make_state([basic_entry("old-0")]),
        watermark=1,
        journal=[
            projection_delta(2, {"op": "append_entry", "entry": basic_entry("old-1")}),
            projection_delta(3, {"op": "append_entry", "entry": basic_entry("old-2")}),
        ],
    )
    store.publish(
        generation=GEN2,
        snapshot_sequence=5,
        state=make_state([basic_entry("new")]),
        watermark=4,
    )

    view = load_unified_store(store.current, at_sequence=1, generation_hint=GEN1)
    assert view is not None
    assert view.generation == GEN1
    assert view.sequence == 1
    assert [entry["id"] for entry in view.snapshot["history"]["entries"]] == [
        "old-0",
        "old-1",
    ]


@pytest.mark.parametrize("corruption", ["newer_minor", "manifest_digest", "journal_digest"])
def test_reader_fails_closed_on_unknown_or_corrupt_storage(store: Store, corruption: str) -> None:
    publish_default(
        store,
        journal=[projection_delta(2, {"op": "append_entry", "entry": basic_entry("entry-1")})],
    )
    expected: type[Exception] = UnifiedStoreError
    if corruption == "newer_minor":
        pointer = read_object(store.current)
        pointer["store_format_minor"] = 5
        store._write(store.current, pointer)
        expected = UnifiedStoreRequiresNewer
    elif corruption == "manifest_digest":
        manifest = store.generation_dir(GEN1) / "manifest.json"
        manifest.write_bytes(manifest.read_bytes().replace(b"quiescent", b"recoverable"))
    else:
        journal = store.session_root / "journal" / f"{1:016d}.jsonl"
        record = read_object(journal)
        record["payload"]["watermark"] = 9
        store._write(journal, record)

    with pytest.raises(expected):
        load_unified_store(store.current)


def test_torn_journal_tail_is_ignored(store: Store) -> None:
    publish_default(
        store,
        journal=[projection_delta(2, {"op": "append_entry", "entry": basic_entry("entry-1")})],
    )
    journal = store.session_root / "journal" / f"{1:016d}.jsonl"
    journal.write_bytes(journal.read_bytes() + b'{"sequence":2')

    view = load_unified_store(store.current)
    assert view is not None
    assert view.sequence == 1
    assert view.watermark == 2


def public_message(
    entry_id: str,
    role: str,
    text: str,
    *,
    status: str = "completed",
    updated_at: int = 2_000,
    turn_id: str = "turn-1",
    session_id: str = SESSION_ID,
) -> dict[str, Any]:
    return {
        "type": "message",
        "id": entry_id,
        "sessionId": session_id,
        "turnId": turn_id,
        "createdAt": 1_000,
        "updatedAt": updated_at,
        "generationStatus": status,
        "relatedEntryId": None,
        "role": role,
        "content": [{"type": "text", "text": text}],
        "source": "harness",
    }


def public_effect(child_session_id: str | None = None) -> dict[str, Any]:
    detail: dict[str, Any] = {
        "kind": "file_write",
        "toolName": "write_file",
        "input": {"filePath": "/tmp/work/result.txt", "content": "done"},
        "display": {"statusText": "Writing result.txt"},
    }
    if child_session_id is not None:
        detail = {
            "kind": "subagent",
            "toolName": "subagent",
            "input": {"task": "inspect", "agent": "reviewer"},
            "childSessionId": child_session_id,
            "display": {"statusText": "Running reviewer"},
        }
    return {
        "type": "effect",
        "id": "effect-1",
        "sessionId": SESSION_ID,
        "turnId": "turn-1",
        "createdAt": 2_100,
        "updatedAt": 2_200,
        "generationStatus": "completed",
        "relatedEntryId": None,
        "title": "write_file" if child_session_id is None else "subagent",
        "detail": detail,
        "state": {
            "status": "completed",
            "output": {"ok": True},
            "outputText": "done",
            "durationMs": 100,
            "display": {"success": True, "message": "done"},
        },
    }


def source_state(entries: list[dict[str, Any]], *, turn_status: str) -> dict[str, Any]:
    state = make_state(entries, status="running" if turn_status == "in_progress" else "idle")
    state["session"]["tokenUsage"] = {
        "inputTokens": 12,
        "outputTokens": 3,
        "cachedInputTokens": 2,
        "totalTokens": 15,
    }
    state["latestTurn"] = {
        "id": "turn-1",
        "sessionId": SESSION_ID,
        "status": turn_status,
        "startedAt": 1_000,
        "completedAt": 2_500 if turn_status == "completed" else None,
    }
    return state


def test_source_projects_mutation_and_resumes_from_checkpoint(store: Store) -> None:
    user = public_message("user-1", "user", "question")
    partial = public_message("assistant-1", "assistant", "draft", status="in_progress")
    store.publish(
        generation=GEN1,
        snapshot_sequence=0,
        state=source_state([user, partial], turn_status="in_progress"),
        watermark=1,
    )
    observer = VibeObserver(root=store.session_root.parent.parent)
    source = observer.open_source(cwd="/tmp/work")
    attached = asyncio.run(source.read())
    assert attached.attached is not None
    assert attached.attached.point is not None and attached.attached.point.position == 1
    source.commit_attachment()
    source.acknowledge_source_checkpoint()

    completed = public_message("assistant-1", "assistant", "answer", updated_at=3_000)
    store.publish(
        generation=GEN2,
        snapshot_sequence=1,
        state=source_state([user, completed], turn_status="completed"),
        watermark=2,
    )
    update = asyncio.run(source.read())
    assert [(event.kind, event.text, event.turn_end) for event in update.events] == [
        (EventKind.ASSISTANT, "answer", True)
    ]
    assert len(update.trajectory) == 1
    assert update.trajectory[0].native_id == "assistant-1"
    assert update.trajectory[0].revision == 2
    assert update.trajectory[0].status is TrajectoryStatus.COMPLETED
    source.acknowledge_source_checkpoint()
    checkpoint = source.source_checkpoint()
    assert checkpoint is not None

    store.publish(
        generation=GEN3,
        snapshot_sequence=2,
        state=source_state([user, completed, public_effect()], turn_status="completed"),
        watermark=3,
    )
    resumed = observer.open_source(
        cwd="/tmp/work",
        session_id=SESSION_ID,
        session_provenance=TranscriptProvenance.EXACT,
        known_location=str(store.current),
        source_checkpoint=checkpoint,
    )
    assert asyncio.run(resumed.read()).attached is not None
    resumed.commit_attachment()
    resumed_update = asyncio.run(resumed.read())
    assert [event.kind for event in resumed_update.events] == [
        EventKind.TOOL_CALL,
        EventKind.TOOL_RESULT,
    ]
    assert [fact.kind for fact in resumed_update.trajectory] == [
        TrajectoryKind.TOOL_CALL,
        TrajectoryKind.TOOL_RESULT,
    ]
    assert {fact.revision for fact in resumed_update.trajectory} == {3}


def test_expired_checkpoint_rebaselines_without_replaying_entries(store: Store) -> None:
    state = source_state([public_message("user-1", "user", "question")], turn_status="completed")
    store.publish(generation=GEN1, snapshot_sequence=0, state=state, watermark=1)
    observer = VibeObserver(root=store.session_root.parent.parent)
    source = observer.open_source(cwd="/tmp/work")
    assert asyncio.run(source.read()).attached is not None
    source.commit_attachment()
    source.acknowledge_source_checkpoint()
    checkpoint = source.source_checkpoint()

    store.publish(
        generation=GEN2,
        snapshot_sequence=2,
        state=source_state(
            [*state["history"]["entries"], public_message("assistant-1", "assistant", "answer")],
            turn_status="completed",
        ),
        watermark=2,
    )
    shutil.rmtree(store.generation_dir(GEN1))
    resumed = observer.open_source(
        cwd="/tmp/work", known_location=str(store.current), source_checkpoint=checkpoint
    )
    assert asyncio.run(resumed.read()).attached is not None
    resumed.commit_attachment()
    resumed.acknowledge_source_checkpoint()
    assert asyncio.run(resumed.read()).error_code == "vibe_unified_checkpoint_expired"
    assert asyncio.run(resumed.read()).events == ()


def test_history_cursor_pages_fresh_source_without_attaching(store: Store) -> None:
    messages = [public_message(f"message-{index}", "user", f"text-{index}") for index in range(3)]
    store.publish(
        generation=GEN1,
        snapshot_sequence=0,
        state=source_state(messages, turn_status="completed"),
        watermark=3,
    )
    source = VibeObserver(root=store.session_root.parent.parent).open_source(cwd="/tmp/work")
    newest = asyncio.run(source.history_page(limit=1))
    older = asyncio.run(source.history_page(before=newest.older_cursor, limit=1))

    assert [event.text for event in newest.events] == ["text-2"]
    assert newest.older_cursor is not None
    assert older.error_code is None
    assert [event.text for event in older.events] == ["text-1"]


def test_exact_malformed_store_does_not_fall_back_to_legacy(store: Store) -> None:
    legacy = store.session_root.parent.parent / "session_20260910_000000_sess"
    legacy.mkdir()
    (legacy / "messages.jsonl").write_text('{"role":"user","content":"legacy"}\n')
    store.current.write_text("{")
    observer = VibeObserver(root=store.session_root.parent.parent)

    assert observer.find_transcript(cwd="/tmp/work", session_id=SESSION_ID) == store.current
    batch = asyncio.run(observer.open_source(cwd="/tmp/work", session_id=SESSION_ID).read())
    assert batch.error_code == "vibe_unified_store_invalid"


def test_child_lineage_and_logical_floor(store: Store) -> None:
    child_id = "child-1"
    store.publish(
        generation=GEN1,
        snapshot_sequence=0,
        state=make_state([public_effect(child_id)]),
        watermark=1,
    )
    child = Store(store.session_root.parent.parent, child_id)
    child.publish(
        generation=GEN1,
        snapshot_sequence=0,
        state=make_state([], session_id=child_id),
        watermark=1,
        parent_session_id=SESSION_ID,
    )
    observer = VibeObserver(root=store.session_root.parent.parent)

    assert observer.find_unified_transcript(cwd="/tmp/work") == store.current
    child_candidate = next(
        candidate
        for candidate in observer.transcript_candidates(cwd="/tmp/work")
        if candidate.session_id == child_id
    )
    assert child_candidate.rejection_reason is not None
    assert [item.session_id for item in observer.native_children(store.current)] == [child_id]
    floor = _vibe_stream_floor(StreamFloorContext(location=str(store.current)))
    assert floor is not None
    assert floor.records is None
    assert floor.stream_id is not None and floor.stream_id.startswith("vibe-unified:")
    assert floor.position == 1
