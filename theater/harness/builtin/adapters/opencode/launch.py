"""OpenCode launch, resume, and model discovery."""

# mypy: disable-error-code=attr-defined

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from theater.harness.base import (
    APPROVALS,
    SERVER_NAME,
    Harness,
    LaunchPlan,
    ResumeLaunchOverlay,
    theater_binary,
)
from theater.models import BadRequest

from .constants import CORRELATION_PLUGIN_SUFFIX, CORRELATION_RECEIPT_SUFFIX, MODELS_TIMEOUT
from .observer import OpenCodeObserver

if TYPE_CHECKING:
    from theater.models import Participant


def _plugin_path(config_path: Path) -> Path:
    return config_path.with_suffix(CORRELATION_PLUGIN_SUFFIX)


def _receipt_path(config_path: Path) -> Path:
    return config_path.with_suffix(CORRELATION_RECEIPT_SUFFIX)


def _correlation_plugin(participant_id: str, receipt_path: Path) -> str:
    """Generate the process-local root-session receipt hook."""
    participant = json.dumps(participant_id)
    receipt = json.dumps(str(receipt_path))
    return f"""import {{ rename, writeFile }} from "node:fs/promises"

const participantID = {participant}
const receipt = {receipt}

async function publish(body) {{
  const pending = `${{receipt}}.${{process.pid}}.tmp`
  await writeFile(pending, JSON.stringify(body) + "\\n", "utf8")
  await rename(pending, receipt)
}}

export const TheaterSessionReceipt = async () => {{
  try {{
    await publish({{ participant_id: participantID, ready: true }})
  }} catch (error) {{
    console.error("theater session receipt failed to initialize", error)
  }}
  return {{
    event: async ({{ event }}) => {{
      if (event.type !== "session.created" || event.properties.info.parentID) return
      try {{
        await publish({{
          participant_id: participantID,
          session_id: event.properties.info.id,
        }})
      }} catch (error) {{
        console.error("theater session receipt failed to publish", error)
      }}
    }},
  }}
}}
"""


class OpenCodeHarness(Harness):
    name = "opencode"
    binary = "opencode"
    icon = "\u25c7"
    aliases = ("open-code", "open_code", "OpenCode", "opencode-ai")
    resume_strategy = "fork"
    resume_takes_prompt: bool = False

    def __init__(self, db: Path | None = None, correlation_dir: Path | None = None):
        self.observer = OpenCodeObserver(db=db, correlation_dir=correlation_dir)

    def plan_launch(
        self,
        *,
        participant_id: str,
        prompt: str,
        config_path: Path,
        approval: str,
        model: str | None = None,
        resume: str | None = None,
    ) -> LaunchPlan:
        if approval not in APPROVALS:
            raise BadRequest(f"approval must be one of {', '.join(APPROVALS)}, got {approval!r}")
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
        receipt_path = _receipt_path(config_path)
        config["plugin"] = [plugin_path.resolve().as_uri()]
        argv = ["opencode"]
        if model:
            argv += ["--model", model]
        if approval == "yolo":
            argv.append("--auto")
        if resume is not None:
            argv += ["-s", resume, "--fork"]
        elif prompt:
            argv += ["--prompt", prompt]
        files = {
            config_path: json.dumps(config, indent=2),
            plugin_path: _correlation_plugin(participant_id, receipt_path),
        }
        return LaunchPlan(argv=argv, env={"OPENCODE_CONFIG": str(config_path)}, files=files)

    def resume_launch_overlay(
        self,
        *,
        predecessor: Participant,
        trusted_session_owners: Sequence[Participant],
    ) -> ResumeLaunchOverlay:
        if predecessor.transcript_domain is None:
            return ResumeLaunchOverlay()
        expected = f"opencode://{self.observer.db.resolve()}"
        if predecessor.transcript_domain != expected:
            raise BadRequest(
                f"cannot resume OpenCode session: predecessor transcript domain "
                f"{predecessor.transcript_domain!r} does not match the OpenCode "
                f"observation domain {expected!r}"
            )
        return ResumeLaunchOverlay(transcript_domain=expected)

    def discover_models(self) -> list[str]:
        try:
            out = subprocess.check_output(
                [self.binary, "models"],
                text=True,
                timeout=MODELS_TIMEOUT,
                stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError as exc:
            raise NotImplementedError(f"{self.binary} is not on PATH") from exc
        except subprocess.TimeoutExpired as exc:
            raise NotImplementedError(
                f"`{self.binary} models` did not answer within {MODELS_TIMEOUT}s"
            ) from exc
        except (OSError, subprocess.SubprocessError) as exc:
            raise NotImplementedError(f"`{self.binary} models` failed: {exc}") from exc
        return [line.strip() for line in out.splitlines() if line.strip()]
