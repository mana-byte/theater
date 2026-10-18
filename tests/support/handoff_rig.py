"""A running source job, a dispatch pump and the cancelled-before-delivery message."""

from __future__ import annotations

import asyncio

#: Every writer of the shared ``cancelled`` code acts before delivery or launch.
CANCELLED_BEFORE_DELIVERY = (
    "Cancelled before delivery or launch; nothing reached the target. Request it again if needed."
)


def handoff_source(daemon, *, name: str = "Writer", prompt: str = "build the parser"):
    participant = daemon.registry.create_spawned(harness="vibe", cwd="/tmp", name=name)
    job = daemon.jobs.create(
        handle=f"{participant.id}#1",
        caller_id="cli",
        target_id=participant.id,
        kind="send",
        prompt=prompt,
    )
    return participant, job


async def pump(daemon, child, state) -> tuple[str, ...]:
    """Let any scheduled dispatch run, then one explicit pass with the target idle."""
    pending = [t for t in daemon.controls._dispatch_tasks.values() if not t.done()]
    await asyncio.sleep(0)
    await asyncio.gather(*pending, return_exceptions=True)
    state.native_turn_id = None
    return (await daemon.controls.dispatch_queue(child.id)).dispatched
