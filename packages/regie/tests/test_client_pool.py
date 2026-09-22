from __future__ import annotations

import pytest
from regie.client_pool import FrontendClientPool

from theater.frontend import FrontendClient


@pytest.mark.asyncio
async def test_real_client_pool_clones_every_concurrent_lane_and_each_action() -> None:
    root = FrontendClient(
        "/tmp/theater.sock",
        client_id="regie-test",
        required_capabilities=("state.v1",),
        request_timeout=7.5,
    )
    pool = FrontendClientPool(root)

    named = (
        pool.state,
        pool.catalog,
        pool.usage,
        pool.bus,
        pool.animation_bus,
        pool.trajectory_query,
        pool.trajectory_follow,
        pool.transcripts,
        pool.controls,
        pool.resume,
    )
    first_action = pool.action_client()
    second_action = pool.action_client()
    first_transcript = pool.transcript_client()
    second_transcript = pool.transcript_client()

    isolated = (*named, first_action, second_action, first_transcript, second_transcript)
    assert len({id(client) for client in (*isolated, root)}) == 15
    assert len({client.config.client_id for client in isolated}) == 14
    assert first_action.config.client_id == "regie-test:action-1"
    assert second_action.config.client_id == "regie-test:action-2"
    assert first_transcript.config.client_id == "regie-test:transcript-1"
    assert second_transcript.config.client_id == "regie-test:transcript-2"
    for client in isolated:
        assert client.config.socket_path == root.config.socket_path
        assert client.config.role == root.config.role
        assert client.config.channel == root.config.channel
        assert client.config.required_capabilities == root.config.required_capabilities
    assert {client.config.client_id: client.config.request_timeout for client in isolated} == {
        "regie-test:state": 35.0,
        "regie-test:catalog": 7.5,
        "regie-test:usage": 7.5,
        "regie-test:bus": 7.5,
        "regie-test:animation_bus": 7.5,
        "regie-test:trajectory_query": 7.5,
        "regie-test:trajectory_follow": 35.0,
        "regie-test:transcripts": 35.0,
        "regie-test:controls": 7.5,
        "regie-test:resume": 7.5,
        "regie-test:action-1": 35.0,
        "regie-test:action-2": 35.0,
        "regie-test:transcript-1": 7.5,
        "regie-test:transcript-2": 7.5,
    }

    await first_action.close()
    await second_action.close()
    await first_transcript.close()
    await second_transcript.close()
    await pool.close()
