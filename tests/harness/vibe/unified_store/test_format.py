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
from theater.harness.contracts.events import TurnTerminal


def read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_bytes())
    assert isinstance(value, dict)
    return value


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
