"""Helpers for tests that start a real runtime backend process and watch it exit."""

from __future__ import annotations

import asyncio
import os

from theater.harness.observation import TranscriptObserver


def pid_alive(pid: int | None) -> bool:
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def await_reaped(pid: int | None, timeout: float = 5.0) -> None:
    for _ in range(int(timeout / 0.02)):
        if not pid_alive(pid):
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"pid {pid} is still alive; the backend was not terminated")


class NoTranscriptObserver(TranscriptObserver):
    """A transcript-less observer: the live source is the only wiring."""

    has_transcript = False

    def find_transcript(self, *, cwd, session_id=None, after=None):  # pragma: no cover
        return None

    def session_id(self, transcript):  # pragma: no cover
        return None

    def parse(self, line, index, *, clip_text=True):  # pragma: no cover
        return []

    def is_idle_screen(self, capture):  # pragma: no cover
        return False


def fake_create(runtime):
    async def create():
        return runtime

    return create
