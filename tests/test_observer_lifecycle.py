"""Watch admission is rechecked after asynchronous teardown and scheduling."""

import asyncio

import pytest
from shipped import VibeHarness

from theater.daemon.observation.service import Observer


@pytest.mark.parametrize("retirement", ["dead", "shutdown"])
async def test_restart_does_not_reopen_after_retirement(registry, tmp_path, retirement):
    participant = registry.register(harness="vibe", pane=None, cwd=str(tmp_path))
    observer = Observer(registry, {"vibe": VibeHarness(root=tmp_path)})
    entered = asyncio.Event()
    cleaning = asyncio.Event()
    release = asyncio.Event()
    starts = []

    async def watch(pid, _harness):
        starts.append(pid)
        entered.set()
        try:
            await asyncio.Future()
        finally:
            cleaning.set()
            await release.wait()

    observer._watch = watch
    try:
        observer._start_watch(participant.id)
        await entered.wait()
        observer._on_live_change(participant.id)
        await cleaning.wait()
        if retirement == "dead":
            registry.mark_dead(participant.id)
        else:
            observer._stopping.set()
        release.set()
        await asyncio.gather(*tuple(observer._restarts))
        assert starts == [participant.id]
        assert participant.id not in observer._tasks
    finally:
        release.set()
        await observer.aclose()


async def test_scheduled_watch_does_not_open_a_retired_source(registry, tmp_path):
    participant = registry.register(harness="vibe", pane=None, cwd=str(tmp_path))
    opened = []

    def source_factory(*_args, **_kwargs):
        opened.append(participant.id)

    observer = Observer(
        registry, {"vibe": VibeHarness(root=tmp_path)}, source_factory=source_factory
    )
    try:
        observer._start_watch(participant.id)
        registry.mark_dead(participant.id)
        await observer._tasks[participant.id]
        assert opened == []
    finally:
        await observer.aclose()


async def test_rebuild_timing_does_not_repeat_birth_readiness(registry, tmp_path, caplog):
    observer = Observer(registry, {"vibe": VibeHarness(root=tmp_path)})
    participant = registry.register(harness="vibe", pane=None, cwd=str(tmp_path))
    entered = asyncio.Event()

    async def watch(_pid, _harness):
        entered.set()
        await asyncio.Future()

    observer._watch = watch
    caplog.set_level("DEBUG", logger="theater.timing")
    try:
        observer._start_watch(participant.id)
        await entered.wait()
        observer._on_live_change(participant.id)
        await asyncio.gather(*tuple(observer._restarts))
        operations = [getattr(record, "theater.operation", None) for record in caplog.records]
        assert operations.count("OBSERVER_WATCH") == 1
        assert operations.count("OBSERVER_RESTART") == 1
    finally:
        await observer.aclose()

    restarted = Observer(registry, {"vibe": VibeHarness(root=tmp_path)})
    restarted._watch = watch
    try:
        restarted._start_watch(participant.id)
        operations = [getattr(record, "theater.operation", None) for record in caplog.records]
        assert operations.count("OBSERVER_WATCH") == 1
    finally:
        await restarted.aclose()


async def test_finished_watch_rebuild_records_birth_readiness_once(
    registry, tmp_path, monkeypatch, caplog
):
    observer = Observer(registry, {"vibe": VibeHarness(root=tmp_path)})
    participant = registry.register(harness="vibe", pane=None, cwd=str(tmp_path))
    readiness: list[str] = []
    starts = 0

    async def watch(_pid, _harness):
        nonlocal starts
        starts += 1
        if starts > 1:
            await asyncio.Future()

    monkeypatch.setattr(
        "theater.daemon.observation.supervision.timing.ready_lag",
        lambda _operation, participant_id, *_args, **_kwargs: readiness.append(participant_id),
    )
    observer._watch = watch
    caplog.set_level("DEBUG", logger="theater.timing")
    try:
        observer._start_watch(participant.id)
        await observer._tasks[participant.id]
        observer._tasks.pop(participant.id)

        await observer._restart_watch(participant.id)

        assert readiness == [participant.id]
        operations = [getattr(record, "theater.operation", None) for record in caplog.records]
        assert operations.count("OBSERVER_RESTART") == 1
    finally:
        await observer.aclose()


async def test_first_watch_from_live_change_is_not_a_restart(registry, tmp_path, caplog):
    observer = Observer(registry, {"vibe": VibeHarness(root=tmp_path)})
    participant = registry.register(harness="vibe", pane=None, cwd=str(tmp_path))

    async def watch(_pid, _harness):
        await asyncio.Future()

    observer._watch = watch
    caplog.set_level("DEBUG", logger="theater.timing")
    try:
        observer._on_live_change(participant.id)
        await asyncio.gather(*tuple(observer._restarts))

        operations = [getattr(record, "theater.operation", None) for record in caplog.records]
        assert operations.count("OBSERVER_WATCH") == 1
        assert operations.count("OBSERVER_RESTART") == 0
    finally:
        await observer.aclose()


async def test_dead_participant_releases_readiness_bookkeeping(registry, tmp_path):
    observer = Observer(registry, {"vibe": VibeHarness(root=tmp_path)})
    participant = registry.register(harness="vibe", pane=None, cwd=str(tmp_path))

    async def watch(_pid, _harness):
        await asyncio.Future()

    observer._watch = watch
    try:
        observer._start_watch(participant.id)
        assert participant.id in observer._readiness_recorded

        registry.mark_dead(participant.id)
        observer._reconcile()
        await asyncio.gather(*tuple(observer._restarts))

        assert participant.id not in observer._readiness_recorded
    finally:
        await observer.aclose()
