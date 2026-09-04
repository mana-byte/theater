from __future__ import annotations

import sys
from pathlib import Path

from theater.mcp_plugins import McpLaunchPlan
from theater.plugin_client import TheaterPluginClient


def plan(context) -> McpLaunchPlan:
    return McpLaunchPlan(
        command=sys.executable,
        argv=(str(Path(__file__)), context.config["label"]),
    )


async def participants():
    async with TheaterPluginClient() as client:
        return await client.list_participants()
