"""Claude Code launch and receipt-hook settings.

MCP config carries the participant id without excluding user servers.
"""

from __future__ import annotations

import json
import shlex
import uuid
from collections.abc import Mapping
from pathlib import Path

from theater import paths
from theater.constants.cli import TRANSCRIPT_RECEIPT_COMMAND
from theater.harness.base import (
    APPROVALS,
    SERVER_NAME,
    LaunchPlan,
    ResumeLaunchOverlay,
    theater_binary,
)
from theater.harness.contracts.callbacks import LaunchContext, ResumeContext
from theater.harness.contracts.manifest import LaunchManifest
from theater.harness.transcript.discovery import root_domain_overlay

from .constants import CLAUDE_RECEIPT_EVENTS


def _claude_settings_path(participant_id: str) -> Path:
    """Launch-specific Claude settings for receipt hooks."""
    return paths.home() / "claude" / f"{participant_id}.settings.json"


def _receipt_hook_command(participant_id: str, token_path: Path) -> str:
    """Command run by Claude lifecycle hooks."""
    return shlex.join(
        [
            theater_binary(),
            TRANSCRIPT_RECEIPT_COMMAND,
            "--id",
            participant_id,
            "--token-file",
            str(token_path),
        ]
    )


def _hook_string(data: Mapping[str, object], *names: str) -> str | None:
    """Extract the first non-empty string value for any of ``names``."""
    for name in names:
        value = data.get(name)
        if isinstance(value, str) and value:
            return value
    return None


def _claude_receipt_settings(participant_id: str, token_path: Path) -> dict:
    """Build launch-local receipt hooks without modifying user settings.

    SessionStart covers starts and rotations; PreCompact preserves the old location.
    Stop is excluded because it does not prove a new transcript location.
    """
    hook = {"type": "command", "command": _receipt_hook_command(participant_id, token_path)}
    entry = {"hooks": [hook]}
    return {"hooks": {event: [entry] for event in CLAUDE_RECEIPT_EVENTS}}


def plan_launch(context: LaunchContext) -> LaunchPlan:
    """Build Claude's launch plan and isolated receipt-hook settings."""
    config = {
        "mcpServers": {
            SERVER_NAME: {
                "command": theater_binary(),
                "args": ["mcp", "--id", context.participant_id],
            }
        }
    }
    settings_path = _claude_settings_path(context.participant_id)
    token_path = paths.observation_dir("claude", context.participant_id) / "receipt-token"
    argv = ["claude", f"--mcp-config={context.config_path}", f"--settings={settings_path}"]
    native_session_id = str(uuid.uuid4())
    argv.append(f"--session-id={native_session_id}")
    if context.model:
        argv.append(f"--model={context.model}")
    if context.reasoning_effort:
        argv.append(f"--effort={context.reasoning_effort}")
    if context.resume:
        argv += [f"--resume={context.resume}", "--fork-session"]
    if context.approval == "yolo":
        argv.append("--dangerously-skip-permissions")
    elif context.approval == "edits":
        argv += ["--permission-mode", "acceptEdits"]
    if context.prompt:
        argv.append(context.prompt)
    return LaunchPlan(
        argv=argv,
        files={
            context.config_path: json.dumps(config, indent=2) + "\n",
            settings_path: json.dumps(
                _claude_receipt_settings(context.participant_id, token_path), indent=2
            )
            + "\n",
        },
        private_files={},
        session_id=native_session_id,
        receipt_token_path=token_path,
    )


def resume_launch_overlay(context: ResumeContext) -> ResumeLaunchOverlay:
    return _resume_launch_overlay(context, Path.home() / ".claude" / "projects")


def _resume_launch_overlay(context: ResumeContext, root: Path) -> ResumeLaunchOverlay:
    if context.predecessor.transcript_domain is None:
        return ResumeLaunchOverlay()
    return root_domain_overlay(
        context.predecessor, str(root.resolve()), "Claude", resolve_declared=True, noun="root"
    )


LAUNCH = LaunchManifest(
    planner=plan_launch,
    approvals=APPROVALS,
    supports_model=True,
    supports_reasoning_effort=True,
    supports_resume=True,
    resume_planner=resume_launch_overlay,
    resume_strategy="fork",
)
