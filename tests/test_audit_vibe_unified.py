"""Audit tests: bounded incremental polling and durable cumulative usage.

Two behaviours the audit asked for, both scoped to the unified Vibe store:

* polling must be bounded and incremental — an unchanged store costs a
  CURRENT byte comparison and a journal ``lstat``, an appended journal record
  costs only its own bytes, and every anomaly falls back to the full load;
* the session's token totals must be durably projected as one stable record,
  so a cold history page and a live diff agree instead of a warm-viewer delta
  double-counting a reloaded total.

The tests count document-body reads and fingerprint comparisons instead of
wall-clock time, so they stay deterministic.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from test_vibe_unified_store import (
    GEN1,
    GEN2,
    SESSION_ID,
    Store,
    basic_entry,
    canonical,
    make_state,
    projection_delta,
    publish_default,
    sha256_json,
)

from theater.harness.builtin.plugins.vibe import unified_source, unified_store
from theater.harness.builtin.plugins.vibe.observer import VibeObserver
from theater.harness.builtin.plugins.vibe.unified_store import (
    UnifiedStoreError,
    UnifiedStoreReader,
    load_unified_store,
)
from theater.harness.contracts.events import EventKind
from theater.provenance import TranscriptProvenance
from theater.trajectory.enums import TrajectoryKind

SESSION_USAGE_ID = "vibe-unified:session-usage"


@pytest.fixture()
def store(tmp_path: Path) -> Store:
    """A fresh unified-store writer (the shared ``store`` fixture is a daemon Store)."""
    return Store(tmp_path / "store-root")


# --- Helpers -------------------------------------------------------------------


def message_entry(entry_id: str, text: str, *, role: str = "user") -> dict[str, Any]:
    return {
        "id": entry_id,
        "type": "message",
        "role": role,
        "content": [{"type": "text", "text": text}],
        "generationStatus": "completed",
        "createdAt": 1_000,
        "updatedAt": 2_000,
    }


def with_usage(state: dict[str, Any], prompt: int, completion: int, cached: int) -> dict[str, Any]:
    state["session"]["tokenUsage"] = {
        "inputTokens": prompt,
        "outputTokens": completion,
        "cachedInputTokens": cached,
    }
    return state


def journal_path(store: Store) -> Path:
    paths = sorted((store.session_root / "journal").glob("*.jsonl"))
    assert len(paths) == 1
    return paths[0]


def append_journal(store: Store, *records: tuple[str, dict[str, Any]]) -> None:
    """Append chain-continuous journal records to the store's segment."""
    path = journal_path(store)
    lines = [line for line in path.read_bytes().splitlines(keepends=True) if line.endswith(b"\n")]
    previous: str | None = None
    sequence = 0
    if lines:
        last = json.loads(lines[-1])
        sequence = int(last["sequence"])
        previous = str(last["record_sha256"])
    else:
        sequence = int(path.stem) - 1
    out = b""
    for record_type, payload in records:
        sequence += 1
        record: dict[str, Any] = {
            "recovery_journal_record_version": 1,
            "sequence": sequence,
            "previous_record_sha256": previous,
            "type": record_type,
            "payload": payload,
        }
        record["record_sha256"] = sha256_json(record)
        out += canonical(record) + b"\n"
        previous = record["record_sha256"]
    with path.open("ab") as stream:
        stream.write(out)


def bump_mtime(path: Path) -> None:
    """Force a distinct mtime so a same-size rewrite is observable."""
    stat = os.lstat(path)
    os.utime(path, ns=(stat.st_atime_ns + 1_000_000, stat.st_mtime_ns + 1_000_000))


def make_source(store: Store, **kwargs: Any):
    values: dict[str, Any] = {
        "cwd": None,
        "session_id": SESSION_ID,
        "after": None,
        "session_provenance": TranscriptProvenance.EXACT,
        "known_location": str(store.current),
        "source_checkpoint": None,
    }
    values.update(kwargs)
    observer = VibeObserver(root=store.session_root.parent.parent, isolated=True)
    return unified_source.UnifiedVibeSource(observer, **values)


async def attach_and_acknowledge(source) -> None:
    batch = await source.read()
    assert batch.attached is not None
    source.commit_attachment()
    source.acknowledge_source_checkpoint()


class ReadCounter:
    """Count reads through ``unified_store._read_document_body``."""

    def __init__(self, monkeypatch) -> None:
        self.total = 0
        self.chunk_reads = 0
        original = unified_store._read_document_body

        def counted(path: Path, description: str) -> bytes:
            self.total += 1
            if path.parent.name == "chunks":
                self.chunk_reads += 1
            return original(path, description)

        monkeypatch.setattr(unified_store, "_read_document_body", counted)


class FingerprintCounter:
    """Count comparisons through ``unified_source.entry_fingerprint``."""

    def __init__(self, monkeypatch) -> None:
        self.calls = 0
        original = unified_source.entry_fingerprint

        def counted(entry: dict) -> str:
            self.calls += 1
            return original(entry)

        monkeypatch.setattr(unified_source, "entry_fingerprint", counted)


def usage_facts(batch) -> list:
    return [fact for fact in batch.trajectory if fact.kind is TrajectoryKind.USAGE]


def batch_is_empty(batch) -> bool:
    return (
        not batch.events
        and not batch.trajectory
        and not batch.trajectory_events
        and not batch.progressed
        and batch.status is None
        and batch.error_code is None
        and batch.attached is None
    )


# --- Reader: incremental polling ------------------------------------------------


def test_reader_no_change_tick_reads_no_documents(store: Store, monkeypatch) -> None:
    publish_default(store)
    reader = UnifiedStoreReader()
    first = reader.load(store.current)
    assert first.changed_entry_ids is None  # a cold load has no change set

    reads = ReadCounter(monkeypatch)
    second = reader.load(store.current)
    assert isinstance(second, unified_store.UnifiedStoreUpdate)
    assert second.view is first.view
    assert second.baseline is first.view
    assert second.changed_entry_ids == frozenset()
    assert reads.total == 0


def test_reader_consumes_appended_records_without_full_reads(store: Store, monkeypatch) -> None:
    publish_default(store)
    reader = UnifiedStoreReader()
    reader.load(store.current)
    reads = ReadCounter(monkeypatch)
    append_journal(store, projection_delta(2, {"op": "append_entry", "entry": basic_entry("e1")}))

    update = reader.load(store.current)
    assert update.changed_entry_ids == frozenset({"e1"})
    assert update.baseline is not None
    entries = update.view.snapshot["history"]["entries"]
    assert [entry.get("id") for entry in entries] == ["entry-0", "e1"]
    assert update.view.watermark == 2
    assert reads.total == 0  # only the appended journal bytes were touched

    plain = load_unified_store(store.current)
    assert plain is not None
    assert plain.sequence == update.view.sequence
    assert plain.watermark == update.view.watermark
    assert plain.snapshot == update.view.snapshot


def test_reader_falls_back_to_a_full_load_on_truncation(store: Store, monkeypatch) -> None:
    publish_default(store)
    append_journal(
        store,
        projection_delta(2, {"op": "append_entry", "entry": basic_entry("e1")}),
        projection_delta(3, {"op": "append_entry", "entry": basic_entry("e2")}),
    )
    reader = UnifiedStoreReader()
    first = reader.load(store.current)
    assert first.view.watermark == 3

    path = journal_path(store)
    lines = [line for line in path.read_bytes().splitlines(keepends=True) if line.endswith(b"\n")]
    path.write_bytes(lines[0])  # truncated underneath the reader
    reads = ReadCounter(monkeypatch)
    update = reader.load(store.current)
    assert update.changed_entry_ids is None  # a full reload has an unknown change set
    assert update.baseline is None
    entries = update.view.snapshot["history"]["entries"]
    assert [entry.get("id") for entry in entries] == ["entry-0", "e1"]
    assert update.view.watermark == 2
    assert reads.total > 0


def test_reader_fails_closed_on_a_same_size_rewrite(store: Store, monkeypatch) -> None:
    publish_default(store)
    append_journal(store, projection_delta(2, {"op": "append_entry", "entry": basic_entry("e1")}))
    reader = UnifiedStoreReader()
    reader.load(store.current)

    path = journal_path(store)
    body = path.read_bytes()
    marker = b'"text":"e1"'
    assert marker in body
    path.write_bytes(body.replace(marker, b'"text":"e9"'))  # same size, new bytes
    bump_mtime(path)
    with pytest.raises(UnifiedStoreError):
        reader.load(store.current)


def test_reader_resets_between_sessions(tmp_path: Path, monkeypatch) -> None:
    first_store = Store(tmp_path / "a")
    publish_default(first_store)
    second_store = Store(tmp_path / "b")
    publish_default(second_store)
    reader = UnifiedStoreReader()
    reader.load(first_store.current)
    reads = ReadCounter(monkeypatch)

    update = reader.load(second_store.current)
    assert update.view.session_id == second_store.session_id
    assert update.changed_entry_ids is None
    assert reads.total > 0  # the second session starts cold


def test_chunk_cache_reuses_generation_chunk_bodies(store: Store, monkeypatch) -> None:
    publish_default(store, pooled=True)
    reader = UnifiedStoreReader()
    cache = reader.chunk_cache
    assert load_unified_store(store.current, chunk_cache=cache) is not None

    reads = ReadCounter(monkeypatch)
    assert load_unified_store(store.current, chunk_cache=cache) is not None
    assert reads.chunk_reads == 0  # the chunk bodies were already verified

    plain_reads = ReadCounter(monkeypatch)
    assert load_unified_store(store.current) is not None
    assert plain_reads.chunk_reads > 0  # without the cache they are re-read


# --- Source: bounded polling ----------------------------------------------------


async def test_source_unchanged_poll_is_free(store: Store, monkeypatch) -> None:
    publish_default(store)
    source = make_source(store)
    await attach_and_acknowledge(source)

    reads = ReadCounter(monkeypatch)
    prints = FingerprintCounter(monkeypatch)
    batch = await source.read()
    assert batch_is_empty(batch)
    assert reads.total == 0
    assert prints.calls == 0


async def test_source_append_skips_untouched_fingerprints(store: Store, monkeypatch) -> None:
    publish_default(store)
    source = make_source(store)
    await attach_and_acknowledge(source)

    reads = ReadCounter(monkeypatch)
    prints = FingerprintCounter(monkeypatch)
    append_journal(
        store,
        projection_delta(2, {"op": "append_entry", "entry": message_entry("e1", "hello")}),
    )
    batch = await source.read()
    assert [event.kind for event in batch.events] == [EventKind.USER]
    assert batch.progressed is True
    assert reads.total == 0
    assert prints.calls == 0  # an append has no prior occurrence to compare
    source.acknowledge_source_checkpoint()

    append_journal(
        store,
        projection_delta(
            3, {"op": "replace_entry", "id": "e1", "entry": message_entry("e1", "hi")}
        ),
    )
    prints = FingerprintCounter(monkeypatch)
    batch = await source.read()
    assert not batch.events  # a reworded message emits no new bus event
    source.acknowledge_source_checkpoint()
    assert prints.calls == 2  # one touched entry: prior and current, once each


async def test_source_generation_rollover_full_reloads(store: Store, monkeypatch) -> None:
    publish_default(store)
    source = make_source(store)
    await attach_and_acknowledge(source)

    reads = ReadCounter(monkeypatch)
    store.publish(
        generation=GEN2,
        snapshot_sequence=1,
        state=make_state([basic_entry("entry-0"), basic_entry("entry-1")]),
        watermark=2,
    )
    batch = await source.read()
    assert batch.progressed is True
    assert reads.total > 0  # a new generation is a full load, not a consume
    view = await source._load_fresh(store.current)
    assert view is not None
    assert view.generation == GEN2
    plain = load_unified_store(store.current)
    assert plain is not None
    assert plain.generation == GEN2


# --- Durable cumulative usage ---------------------------------------------------


async def test_history_page_carries_one_cumulative_usage_record(store: Store) -> None:
    state = with_usage(make_state([message_entry("e0", "hello")]), 10, 5, 2)
    store.publish(generation=GEN1, snapshot_sequence=0, state=state, watermark=1)
    source = make_source(store)

    page = await source.history_page()
    assert page.error_code is None
    usage = [fact for fact in page.trajectory if fact.kind is TrajectoryKind.USAGE]
    assert len(usage) == 1
    fact = usage[0]
    assert fact.native_id == SESSION_USAGE_ID
    assert fact.request_id is None
    assert fact.revision == 1
    assert fact.usage is not None
    assert fact.usage.input_tokens == 8  # prompt minus cache reads
    assert fact.usage.output_tokens == 5
    assert fact.usage.cache_read_tokens == 2


async def test_live_diff_and_history_agree_on_session_totals(store: Store) -> None:
    state = with_usage(make_state([message_entry("e0", "hello")]), 10, 5, 2)
    store.publish(generation=GEN1, snapshot_sequence=0, state=state, watermark=1)
    source = make_source(store)
    await attach_and_acknowledge(source)

    bumped = with_usage(make_state([message_entry("e0", "hello")]), 20, 9, 2)
    append_journal(
        store,
        projection_delta(2, {"op": "set_envelope", "state": bumped}),
    )
    batch = await source.read()
    # The live delta event still feeds the per-transition usage pipeline.
    delta = [event for event in batch.events if event.usage is not None]
    assert len(delta) == 1
    assert delta[0].usage is not None
    assert delta[0].usage.idempotency_key == "vibe-unified:10:5:2->20:9:2"
    usage = usage_facts(batch)
    assert len(usage) == 1
    assert usage[0].native_id == SESSION_USAGE_ID
    assert usage[0].revision == 2
    assert usage[0].usage is not None
    assert (usage[0].usage.input_tokens, usage[0].usage.output_tokens) == (18, 9)
    source.acknowledge_source_checkpoint()

    # A cold page of the same session carries the same record the live diff
    # emitted: same stable id, same revision — merged caches keep one.
    page = await source.history_page()
    assert page.error_code is None
    page_usage = [fact for fact in page.trajectory if fact.kind is TrajectoryKind.USAGE]
    assert len(page_usage) == 1
    assert page_usage[0].native_id == usage[0].native_id
    assert page_usage[0].revision == usage[0].revision
    assert page_usage[0].usage == usage[0].usage


async def test_session_reset_emits_no_delta_and_honest_totals(store: Store) -> None:
    state = with_usage(make_state([message_entry("e0", "hello")]), 10, 5, 2)
    store.publish(generation=GEN1, snapshot_sequence=0, state=state, watermark=1)
    source = make_source(store)
    await attach_and_acknowledge(source)

    reset = with_usage(make_state([message_entry("e0", "hello")]), 4, 1, 0)
    append_journal(store, projection_delta(2, {"op": "set_envelope", "state": reset}))
    batch = await source.read()
    assert [event for event in batch.events if event.usage is not None] == []
    usage = usage_facts(batch)
    assert len(usage) == 1
    assert usage[0].usage is not None
    assert (usage[0].usage.input_tokens, usage[0].usage.output_tokens) == (4, 1)
    assert usage[0].usage.cache_read_tokens == 0
    source.acknowledge_source_checkpoint()

    page = await source.history_page()
    page_usage = [fact for fact in page.trajectory if fact.kind is TrajectoryKind.USAGE]
    assert len(page_usage) == 1
    assert page_usage[0].usage == usage[0].usage


async def test_history_page_reserves_a_slot_for_the_usage_record(store: Store) -> None:
    state = with_usage(
        make_state([message_entry("e0", "first"), message_entry("e1", "second")]), 10, 5, 2
    )
    store.publish(generation=GEN1, snapshot_sequence=0, state=state, watermark=1)
    source = make_source(store)

    page = await source.history_page(limit=2)
    assert page.error_code is None
    kinds = [fact.kind for fact in page.trajectory]
    # The reserved usage slot keeps the page honest: the older entry moves to
    # the next page instead of being sliced off and never delivered at all.
    assert kinds.count(TrajectoryKind.USAGE) == 1
    assert kinds.count(TrajectoryKind.USER) == 1
    assert page.has_older is True
    assert page.older_cursor is not None

    older = await source.history_page(before=page.older_cursor, limit=2)
    assert older.error_code is None
    older_kinds = [fact.kind for fact in older.trajectory]
    assert older_kinds.count(TrajectoryKind.USER) == 1
    assert older_kinds.count(TrajectoryKind.USAGE) == 1
