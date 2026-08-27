"""OpenCode launch, resume, and model discovery."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from pathlib import Path

from theater import paths
from theater.harness.base import SERVER_NAME, theater_binary
from theater.harness.contracts.callbacks import (
    LaunchContext,
    ModelDiscoveryContext,
    ResumeContext,
)
from theater.harness.contracts.launch import LaunchPlan, ResumeLaunchOverlay
from theater.harness.transcript.discovery import root_domain_overlay

from .constants import (
    CORRELATION_PLUGIN_SUFFIX,
    DB_NAME,
    MODELS_TIMEOUT,
    RECEIPT_RETRY_DELAYS_MS,
)
from .observer import data_dir


def _plugin_path(config_path: Path) -> Path:
    return config_path.with_suffix(CORRELATION_PLUGIN_SUFFIX)


def _correlation_plugin(participant_id: str, token_path: Path) -> str:
    """Generate the process-local root-session receipt hook."""
    participant = json.dumps(participant_id)
    token = json.dumps(str(token_path))
    command = json.dumps(theater_binary())
    retry_delays = json.dumps(RECEIPT_RETRY_DELAYS_MS)
    return f"""import {{ spawn }} from "node:child_process"

const participantID = {participant}
const tokenPath = {token}
const theater = {command}
const retryDelays = {retry_delays}
let currentSessionID = null
let deliveredSessionID = null
let publishing = false
let generation = 0

function publish(sessionID) {{
  return new Promise((resolve) => {{
    let settled = false
    const finish = (ok) => {{
      if (settled) return
      settled = true
      resolve(ok)
    }}
    try {{
      const child = spawn(
        theater,
        ["transcript-receipt", "--strict-exit", "--id", participantID, "--token-file", tokenPath],
        {{ stdio: ["pipe", "ignore", "ignore"] }},
      )
      child.once("error", () => finish(false))
      child.once("close", (code) => finish(code === 0))
      child.stdin.once("error", () => finish(false))
      child.stdin.end(JSON.stringify({{ session_id: sessionID }}))
    }} catch {{
      finish(false)
    }}
  }})
}}

const sleep = (delay) => new Promise((resolve) => setTimeout(resolve, delay))

async function deliver(sessionID, version) {{
  for (const delay of retryDelays) {{
    if (version !== generation) return
    if (delay > 0) await sleep(delay)
    if (version !== generation) return
    if (await publish(sessionID)) {{
      if (version === generation) deliveredSessionID = sessionID
      return
    }}
  }}
}}

function schedule() {{
  if (!currentSessionID || deliveredSessionID === currentSessionID || publishing) return
  const sessionID = currentSessionID
  const version = generation
  publishing = true
  void deliver(sessionID, version).finally(() => {{
    publishing = false
    if (version !== generation) schedule()
  }})
}}

export const TheaterSessionReceipt = async () => {{
  return {{
    event: async ({{ event }}) => {{
      try {{
        const info = event?.properties?.info
        if (event.type === "session.created" && info && !info.parentID) {{
          if (typeof info.id !== "string" || !info.id) return
          if (info.id !== currentSessionID) {{
            currentSessionID = info.id
            deliveredSessionID = null
            generation += 1
          }}
        }}
        schedule()
      }} catch {{}}
    }},
  }}
}}
"""


def plan_launch(context: LaunchContext) -> LaunchPlan:
    participant_id = context.participant_id
    config_path = context.config_path
    config = {
        "$schema": "https://opencode.ai/config.json",
        "mcp": {
            SERVER_NAME: {
                "type": "local",
                "enabled": True,
                "command": [theater_binary(), "mcp", "--id", participant_id],
            }
        },
    }
    plugin_path = _plugin_path(config_path)
    token_path = paths.observation_dir("opencode", participant_id) / "receipt-token"
    config["plugin"] = [plugin_path.resolve().as_uri()]
    argv = ["opencode"]
    if context.model:
        argv += ["--model", context.model]
    if context.approval == "yolo":
        argv.append("--auto")
    if context.resume is not None:
        argv += ["-s", context.resume, "--fork"]
    elif context.prompt:
        argv += ["--prompt", context.prompt]
    files = {
        config_path: json.dumps(config, indent=2),
        plugin_path: _correlation_plugin(participant_id, token_path),
    }
    return LaunchPlan(
        argv=argv,
        env={"OPENCODE_CONFIG": str(config_path)},
        files=files,
        receipt_token_path=token_path,
    )


def resume_launch_overlay(context: ResumeContext, *, db: Path | None = None) -> ResumeLaunchOverlay:
    if context.predecessor.transcript_domain is None:
        return ResumeLaunchOverlay()
    expected = f"opencode://{(db or data_dir() / DB_NAME).resolve()}"
    return root_domain_overlay(context.predecessor, expected, "OpenCode")


def discover_models(context: ModelDiscoveryContext) -> Sequence[str]:
    try:
        out = subprocess.check_output(
            [context.binary, "models"],
            text=True,
            timeout=MODELS_TIMEOUT,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError as exc:
        raise NotImplementedError(f"{context.binary} is not on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise NotImplementedError(
            f"`{context.binary} models` did not answer within {MODELS_TIMEOUT}s"
        ) from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise NotImplementedError(f"`{context.binary} models` failed: {exc}") from exc
    return [line.strip() for line in out.splitlines() if line.strip()]
