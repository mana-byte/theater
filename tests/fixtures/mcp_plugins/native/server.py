from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from mcp.server import MCPServer

from theater.mcp_plugins import McpLaunchPlan
from theater.plugin_client import TheaterPluginClient


def plan(context) -> McpLaunchPlan:
    return McpLaunchPlan(
        command=sys.executable,
        argv=(str(Path(__file__)),),
        env={"FIXTURE_NATIVE_LABEL": context.config["label"]},
    )


class FixtureTransport:
    def __init__(self, response: list[dict]) -> None:
        self.response = response

    async def call(self, method: str, **params):
        if method != "plugin.call" or params["operation"] != "participants.list":
            raise RuntimeError("unexpected fixture operation")
        return self.response


async def participants():
    raw = os.environ.get("THEATER_PLUGIN_FIXTURE_RESPONSE")
    client = FixtureTransport(json.loads(raw)) if raw is not None else None
    kwargs = {} if client is None else {"client": client}
    async with TheaterPluginClient(**kwargs) as client:
        return await client.list_participants()


def build() -> MCPServer:
    server = MCPServer("fixture-native")

    @server.tool()
    async def list_fixture_participants() -> list[dict]:
        return await participants()

    return server


if __name__ == "__main__":
    build().run()
