"""Changed batches catch up immediately; empty reads cannot spin."""

import asyncio

from regie.controllers.state_follow import StateFollowLoop


async def test_follow_loop_catches_up_then_backs_off_and_closes():
    calls = 0
    caught_up = asyncio.Event()
    errors = []

    async def refresh():
        nonlocal calls
        calls += 1
        if calls == 1:
            return True
        caught_up.set()
        return False

    follower = StateFollowLoop(refresh, retry_delay=60, on_error=errors.append)
    try:
        follower.start()
        follower.start()
        async with asyncio.timeout(1):
            await caught_up.wait()
        await asyncio.sleep(0)
        assert calls == 2
    finally:
        await follower.close()
    follower.start()
    await asyncio.sleep(0)
    assert calls == 2 and not errors
