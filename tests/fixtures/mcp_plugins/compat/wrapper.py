from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from mcp.server import MCPServer

from theater.mcp_plugins import McpLaunchPlan

try:
    from .existing_server import ExistingParticipantServer
except ImportError:
    from existing_server import ExistingParticipantServer


def plan(context) -> McpLaunchPlan:
    return McpLaunchPlan(
        command=sys.executable,
        argv=(str(Path(__file__)),),
        env={"FIXTURE_ENDPOINT": context.config["endpoint"]},
    )


def participant(participant_id: str) -> dict:
    completed = subprocess.run(
        ["theater", "plugin", "call", "participants.get"],
        input=json.dumps({"id": participant_id}),
        capture_output=True,
        check=False,
        text=True,
        env=os.environ,
    )
    response = json.loads(completed.stdout)
    if not response.get("ok"):
        raise RuntimeError(response["error"]["message"])
    return ExistingParticipantServer().participant(participant_id, lambda _id: response["result"])


def build() -> MCPServer:
    server = MCPServer("fixture-compat")

    @server.tool()
    def get_fixture_participant(participant_id: str) -> dict:
        return participant(participant_id)

    return server


if __name__ == "__main__":
    build().run()
