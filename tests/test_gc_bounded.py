"""Bounded journal retirement and reader-friendly WAL maintenance."""

from __future__ import annotations

import asyncio
import sqlite3

from sqlalchemy import update

from theater.daemon.gc import _sweep_journal
from theater.daemon.persistence.checkpoint import passive_checkpoint
from theater.daemon.schema import orchestration_events
from theater.models import JournalEventRecord


async def test_journal_retention_bounds_reads_and_keeps_partial_groups(store):
    with store.write_unit() as unit:
        for size in (300, 300, 500, 1):
            store.journal.append_group(
                unit,
                [
                    JournalEventRecord(
                        kind="catalog.invalidated",
                        entity_id="test",
                        entity_revision=1,
                        payload={},
                        recorded_at=10.0,
                    )
                    for _ in range(size)
                ],
            )
    prefix = store.journal.expired_prefix(cutoff=11.0, limit=500)
    assert (prefix.ending_sequence, prefix.scanned, prefix.expired) == (300, 500, 300)
    assert store.journal.expired_prefix(cutoff=10.0, limit=5000).scanned == 1
    assert await _sweep_journal(store, cutoff=11.0, batch=500) == 1101
    assert store.journal.current_sequence() == 1101


async def test_journal_retention_keeps_malformed_group(store):
    with store.write_unit() as unit:
        store.journal.append_group(
            unit,
            [
                JournalEventRecord(
                    kind="catalog.invalidated",
                    entity_id="test",
                    entity_revision=1,
                    payload={},
                    recorded_at=10.0,
                )
                for _ in range(3)
            ],
        )
    store.conn.execute(update(orchestration_events).values(ending_sequence=2))
    assert await _sweep_journal(store, cutoff=11.0, batch=500) == 0


async def test_passive_checkpoint_does_not_wait_for_pinned_reader(tmp_path):
    path = tmp_path / "checkpoint.db"
    writer = sqlite3.connect(path, isolation_level=None)
    reader = sqlite3.connect(path, isolation_level=None)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("CREATE TABLE test (value INTEGER)")
        writer.execute("INSERT INTO test VALUES (1)")
        reader.execute("BEGIN")
        assert reader.execute("SELECT value FROM test").fetchall() == [(1,)]
        writer.execute("INSERT INTO test VALUES (2)")
        busy, frames, checkpointed = await asyncio.wait_for(
            asyncio.to_thread(passive_checkpoint, path), 1
        )
        assert busy == 0
        assert frames > checkpointed >= 0
        assert reader.execute("SELECT value FROM test").fetchall() == [(1,)]
        reader.execute("ROLLBACK")
        assert writer.execute("SELECT value FROM test").fetchall() == [(1,), (2,)]
    finally:
        reader.close()
        writer.close()
