"""Vibe unified store format: minors, chunk reassembly and fail-closed reads."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from tests.support.vibe_store import (
    GEN1,
    GEN2,
    Store,
    basic_entry,
    canonical,
    make_state,
    projection_delta,
    public_effect,
    public_message,
    publish_default,
    sha256_hex,
    source_state,
)
from theater.harness.builtin.plugins.vibe.observer import VibeObserver
from theater.harness.builtin.plugins.vibe.unified_store import (
    STORE_FORMAT_MINOR,
    UnifiedStoreError,
    UnifiedStoreRequiresNewer,
    load_unified_store,
)
from theater.harness.contracts.events import EventKind, TurnTerminal
from theater.trajectory.enums import TrajectoryKind


def read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_bytes())
    assert isinstance(value, dict)
    return value


@pytest.mark.parametrize("minor", [None, *range(1, STORE_FORMAT_MINOR + 1)])
def test_reader_supports_every_known_store_minor(tmp_path: Path, minor: int | None) -> None:
    store = Store(tmp_path / str(minor))
    publish_default(store, store_minor=minor)
    view = load_unified_store(store.current)
    assert view is not None
    assert view.store_minor == (1 if minor is None else minor)


def test_minor8_pooled_capability_catalog_round_trips(store: Store) -> None:
    """Store minor 8 pools the capability catalog into one chunk; the reader reattaches it."""
    catalog = {
        "agent_types": ["build", "plan"],
        "skills": ["skill-creator"],
        "tool_groups": {"file": ["read", "write"]},
    }
    publish_default(store, store_minor=8, runtime_catalog=catalog)
    envelope = read_object(store.generation_dir(GEN1) / "runtime-state.json")
    assert envelope["core_capabilities"] is None
    view = load_unified_store(store.current)
    assert view is not None and view.store_minor == 8
    assert view.runtime_state["core_capabilities"] == catalog


def test_manifest_rejects_a_multi_chunk_runtime_state(store: Store) -> None:
    """The runtime state pools exactly one catalog chunk; anything else fails closed."""
    publish_default(store, store_minor=8, runtime_catalog={"skills": []})
    manifest_path = store.generation_dir(GEN1) / "manifest.json"
    manifest = read_object(manifest_path)
    digest = manifest["runtime_state"]["chunks"][0]
    manifest["runtime_state"]["chunks"] = [digest, digest]
    body = canonical(manifest)
    manifest_path.write_bytes(body + b"\n")
    pointer = read_object(store.current)
    pointer["manifest_sha256"] = sha256_hex(body)
    store._write(store.current, pointer)
    with pytest.raises(UnifiedStoreError, match="pooled capability catalog must be one chunk"):
        load_unified_store(store.current)


def pruned_settled_action() -> dict[str, Any]:
    """A minor-8 settled tool action: bodies pruned, digests and flags retained."""
    return {
        "action_id": "act-pruned-1",
        "kind": "tool",
        "state": "succeeded",
        "request_sha256": "0" * 64,
        "request": None,
        "lease_id": None,
        "recovery_mode": "idempotent_retry",
        "process_manager_instance_id": None,
        "process_request_sha256": None,
        "prepared_process_start": None,
        "result": None,
        "request_pruned": True,
        "result_pruned": True,
    }


def test_minor8_pruned_action_bodies_are_never_fabricated(store: Store) -> None:
    """Pruned settled-action bodies stay absent, and projection reads only public entries.

    A future consumer that derives tool I/O from runtime-state action bodies would
    silently report absent detail on every minor-8 store; this test fails loudly
    instead of letting the reader fabricate bodies that were pruned.
    """
    user = public_message("user-1", "user", "question")
    partial = public_message("assistant-1", "assistant", "draft", status="in_progress")
    store.publish(
        generation=GEN1,
        snapshot_sequence=0,
        state=source_state([user, partial], turn_status="in_progress"),
        watermark=1,
        store_minor=8,
        runtime_actions=[pruned_settled_action()],
    )
    observer = VibeObserver(root=store.session_root.parent.parent)
    source = observer.open_source(cwd="/tmp/work")
    assert asyncio.run(source.read()).attached is not None
    source.commit_attachment()
    source.acknowledge_source_checkpoint()

    completed = public_message("assistant-1", "assistant", "answer", updated_at=3_000)
    store.publish(
        generation=GEN2,
        snapshot_sequence=1,
        state=source_state([user, completed, public_effect()], turn_status="completed"),
        watermark=2,
        store_minor=8,
        runtime_actions=[pruned_settled_action()],
    )
    view = load_unified_store(store.current)
    assert view is not None and view.store_minor == 8
    (action,) = view.runtime_state["actions"]
    assert action["request"] is None and action["result"] is None
    assert action["request_pruned"] is True and action["result_pruned"] is True

    update = asyncio.run(source.read())
    turn_ends = [event for event in update.events if event.turn_end]
    assert [(event.kind, event.text, event.turn_terminal) for event in turn_ends] == [
        (EventKind.ASSISTANT, "answer", TurnTerminal.COMPLETED)
    ]
    assert [fact.kind for fact in update.trajectory] == [
        TrajectoryKind.ASSISTANT,
        TrajectoryKind.TOOL_CALL,
        TrajectoryKind.TOOL_RESULT,
    ]
    assert all(fact.native_id != "act-pruned-1" for fact in update.trajectory)


def test_unreadable_store_surfaces_as_a_source_error(store: Store) -> None:
    """A store no reader understands fails loudly instead of waiting forever."""
    publish_default(store, store_minor=STORE_FORMAT_MINOR + 1)
    observer = VibeObserver(root=store.session_root.parent.parent)
    error = observer.unified_store_load_error()
    assert isinstance(error, UnifiedStoreRequiresNewer)
    source = observer.open_source(cwd="/tmp/work")
    batch = asyncio.run(source.read())
    assert batch.waiting is True
    assert batch.error_code == "vibe_unified_store_newer"
    assert "upgrade Theater" in (batch.error or "")


def test_transient_store_errors_keep_waiting(store: Store, monkeypatch) -> None:
    """A torn read retries next poll; only structural errors fail the channel."""
    publish_default(store, store_minor=STORE_FORMAT_MINOR + 1)

    def _torn(_path):
        raise OSError("torn read")

    monkeypatch.setattr("theater.harness.builtin.plugins.vibe.identity.load_unified_store", _torn)
    observer = VibeObserver(root=store.session_root.parent.parent)
    assert observer.unified_store_load_error() is None
    source = observer.open_source(cwd="/tmp/work")
    batch = asyncio.run(source.read())
    assert batch.waiting is True
    assert batch.error_code is None


def test_attachment_reports_a_completed_turn_end(store: Store) -> None:
    """Attaching to a completed turn surfaces the turn end, not just the transcript."""
    state = source_state([public_message("asst-1", "assistant", "answer")], turn_status="completed")
    publish_default(store, state=state)
    source = VibeObserver(root=store.session_root.parent.parent).open_source(cwd="/tmp/work")
    attached = asyncio.run(source.read()).attached
    assert attached is not None
    assert attached.last_event is not None
    assert attached.last_event.turn_end is True
    assert attached.last_event.turn_terminal is TurnTerminal.COMPLETED


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


def test_abandoned_command_receipts_replay_as_no_ops(store: Store) -> None:
    publish_default(
        store,
        store_minor=7,
        journal=[
            ("receipt_failed", {"client_command_id": "command-1", "reason": "abandoned"}),
            projection_delta(3, {"op": "append_entry", "entry": basic_entry("entry-1")}),
        ],
    )
    view = load_unified_store(store.current)
    assert view is not None
    assert [entry["id"] for entry in view.snapshot["history"]["entries"]][-1] == "entry-1"


def test_minor9_session_relocation_applies_its_projection_delta(store: Store) -> None:
    publish_default(
        store,
        store_minor=9,
        journal=[
            (
                "session_relocated",
                {
                    "metadata": {"cwd": "/tmp/moved", "origin_cwd": "/tmp/work"},
                    "plugin_lock": {},
                    "watermark": 3,
                    "delta": [{"op": "append_entry", "entry": basic_entry("entry-1")}],
                },
            ),
        ],
    )
    view = load_unified_store(store.current)
    assert view is not None
    assert view.watermark == 3
    assert [entry["id"] for entry in view.snapshot["history"]["entries"]][-1] == "entry-1"


def test_session_relocation_without_its_metadata_is_rejected(store: Store) -> None:
    publish_default(
        store,
        store_minor=9,
        journal=[("session_relocated", {"watermark": 3, "delta": []})],
    )
    with pytest.raises(UnifiedStoreError, match="session_relocated payload"):
        load_unified_store(store.current)


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
        pointer["store_format_minor"] = STORE_FORMAT_MINOR + 1
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
