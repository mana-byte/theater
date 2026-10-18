"""Fixed session ids, pids and the rollout history helper for Codex correlation tests."""

from __future__ import annotations

from pathlib import Path

from theater.harness.builtin.plugins.codex.observer import CodexObserver

# Two sessions born a minute apart, so "newest" is unambiguous and a test that
# means to select the older one is visibly not just getting lucky.
SESSION_A = "01a00cdf-17f9-7851-99a4-b0dbaad18bed"
SESSION_B = "01a00cdf-c496-7f92-8429-d75394c3edfb"
PID_A = 74065
PID_B = 74675


async def history_of(observer: CodexObserver, cwd: Path, **kwargs):
    source = observer.open_source(cwd=str(cwd), **kwargs)
    try:
        return await source.history(last_n=0)
    finally:
        await source.aclose()
