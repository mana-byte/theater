from __future__ import annotations

import json
import subprocess
import sys

from theater.mcp_plugins import McpLaunchPlan


def plan(context) -> McpLaunchPlan:
    return McpLaunchPlan(
        command=sys.executable,
        argv=("-m", "fixture_compat"),
        env={"FIXTURE_ENDPOINT": context.config["endpoint"]},
    )


def participant(participant_id: str, *, credential_file: str) -> dict:
    completed = subprocess.run(
        ["theater", "plugin", "call", "participants.get", "--credential-file", credential_file],
        input=json.dumps({"id": participant_id}),
        capture_output=True,
        check=False,
        text=True,
    )
    response = json.loads(completed.stdout)
    if not response.get("ok"):
        raise RuntimeError(response["error"]["message"])
    return response["result"]
