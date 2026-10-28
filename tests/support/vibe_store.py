"""A writer for Vibe's unified session store, and the turn documents and states tests publish."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import Any

import rfc8785

from theater.harness.builtin.plugins.vibe import unified_store
from theater.harness.builtin.plugins.vibe.observer import VibeObserver

SESSION_ID = "sess-1"


GEN1 = "0" * 15 + "1"


GEN2 = "0" * 15 + "2"


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
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "runtime_state_version": 3,
        "session_id": session_id,
        "snapshot_sequence": snapshot_sequence,
        "session_metadata": {
            "root_session_id": parent_session_id or session_id,
            "parent_session_id": parent_session_id,
            "cwd": "/tmp/work",
            **(metadata or {}),
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
        runtime_catalog: Any = None,
        runtime_actions: list[dict[str, Any]] | None = None,
        session_metadata: dict[str, Any] | None = None,
    ) -> None:
        first_sequence = snapshot_sequence + 1
        checkpoint = {"checkpoint_version": 1, "context": {"messages": state["history"]["entries"]}}
        runtime = make_runtime(
            snapshot_sequence,
            session_id=self.session_id,
            parent_session_id=parent_session_id,
            metadata=session_metadata,
        )
        if runtime_actions is not None:
            runtime["actions"] = runtime_actions
        runtime_chunks = None
        if runtime_catalog is not None:
            runtime["core_capabilities"] = runtime_catalog
            runtime_chunks = self._pool_value(runtime, "core_capabilities")
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
                "chunks": runtime_chunks,
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
            record: dict[str, Any] = {
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

    def _pool_value(self, document: dict[str, Any], key: str) -> list[str]:
        """Pool one whole field as a single-item chunk, leaving null behind."""
        body = canonical([document[key]])
        digest = sha256_hex(body)
        chunk = self.session_root / "chunks" / f"{digest}.json"
        chunk.parent.mkdir(parents=True, exist_ok=True)
        chunk.write_bytes(body + b"\n")
        document[key] = None
        return [digest]

    @staticmethod
    def _write(path: Path, value: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(canonical(value) + b"\n")


def publish_default(store: Store, **overrides: Any) -> None:
    values: dict[str, Any] = {
        "generation": GEN1,
        "snapshot_sequence": 0,
        "state": make_state([basic_entry("entry-0")]),
        "watermark": 1,
    }
    values.update(overrides)
    store.publish(**values)


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
        "kind": "tool",
        "toolName": "file_system.write_file",
        "input": {"path": "/tmp/work/result.txt", "content": "done"},
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
        "title": "file_system.write_file" if child_session_id is None else "subagent",
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


def turn_doc(
    entries: list[dict[str, Any]], turn_id: str, status: str, *, queued: tuple[str, ...] = ()
) -> dict[str, Any]:
    state = make_state(entries, status="running" if status == "in_progress" else "idle")
    state["latestTurn"] = {
        "id": turn_id,
        "sessionId": SESSION_ID,
        "status": status,
        "queueItemId": f"queue-{turn_id}",
        "startedAt": 1_000,
        **({"completedAt": 2_500} if status != "in_progress" else {}),
    }
    state["turnQueue"]["items"] = [
        {"id": item, "createdAt": 1_000, "entries": [{"role": "user", "content": []}]}
        for item in queued
    ]
    return state


def turn_record(watermark: int, turn_id: str, status: str, *entries: dict, **queue: Any):
    appended = ({"op": "append_entry", "entry": entry} for entry in entries)
    envelope = {"op": "set_envelope", "state": turn_doc([], turn_id, status, **queue)}
    return projection_delta(watermark, *appended, envelope)


USER_A = public_message("user-a", "user", "job A prompt", turn_id="turn-a")


ANSWER_A = public_message("asst-a", "assistant", "ANSWER A", turn_id="turn-a")


USER_B = public_message("user-b", "user", "job B prompt", turn_id="turn-b")


RUNNING_A = turn_doc([USER_A], "turn-a", "in_progress")


A_ENDS = turn_record(2, "turn-a", "completed", ANSWER_A)


B_STARTS = turn_record(3, "turn-b", "in_progress", USER_B, queued=("q",))


def attached_to_running_a(store: Store):
    store.publish(generation=GEN1, snapshot_sequence=0, state=RUNNING_A, watermark=1)
    source = VibeObserver(root=store.session_root.parent.parent).open_source(cwd="/tmp/work")
    attached = asyncio.run(source.read())
    assert attached.attached is not None and attached.turn_state is not None
    assert (attached.turn_state.turn_id, attached.turn_state.open) == ("turn-a", True)
    source.commit_attachment()
    source.acknowledge_source_checkpoint()
    return source
