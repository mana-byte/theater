"""Scheduled GC keeps its post-sweep wake policy."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from tests.support.builders import make_job, make_participant
from tests.support.gc_sweep import retention
from theater.daemon.jobs import JobState
from theater.daemon.runtime.maintenance import gc_loop
from theater.models import now


async def test_scheduled_gc_wakes_handoffs_after_marking_a_running_job(daemon):
    make_participant(daemon.store, pid="old-target")
    make_job(
        daemon.store,
        handle="stale-source",
        target_id="old-target",
        state=JobState.RUNNING,
        finished_at=None,
        created_at=now() - 30 * 86400,
    )
    stopping = asyncio.Event()
    wakes = []

    def wake() -> None:
        wakes.append("wake")
        stopping.set()

    scheduled = SimpleNamespace(
        config=SimpleNamespace(retention=retention(interval=0.01)),
        store=daemon.store,
        jobs=daemon.jobs,
        handoff=SimpleNamespace(wake=wake),
        _stopping=stopping,
    )
    await asyncio.wait_for(gc_loop(scheduled), timeout=2)

    assert wakes == ["wake"]
    assert daemon.store.get_job("stale-source").state == JobState.CRASHED
