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
