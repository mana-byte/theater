"""Claude Code launch and receipt-hook settings.

MCP config carries the participant id without excluding user servers.
"""

from __future__ import annotations

import json
import shlex
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, cast

from theater import paths
from theater.harness.base import (
    APPROVALS,
    SERVER_NAME,
    Harness,
    LaunchPlan,
    ResumeLaunchOverlay,
    theater_binary,
)
from theater.harness.transcript.discovery import root_domain_overlay
from theater.models import BadRequest

from .constants import CLAUDE_RECEIPT_COMMAND, CLAUDE_RECEIPT_EVENTS

if TYPE_CHECKING:
    from theater.models import Participant

    from .observer import ClaudeCodeObserver


def _claude_settings_path(participant_id: str) -> Path:
    """Launch-specific Claude settings for receipt hooks."""
    return paths.home() / "claude" / f"{participant_id}.settings.json"


def _receipt_hook_command(participant_id: str, token_path: Path) -> str:
    """Command run by Claude lifecycle hooks."""
    return shlex.join(
        [
            theater_binary(),
            CLAUDE_RECEIPT_COMMAND,
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


class ClaudeCodeHarness(Harness):
    name = "claude"
    binary = "claude"
    binaries = frozenset({".claude-wrapped", "claude-wrapped"})
    icon = "✻"
    aliases = ("claude_code", "claude-code", "Claude", "ClaudeCode")
    resume_strategy = "fork"

    def __init__(self, root: Path | None = None):
        from .observer import ClaudeCodeObserver

        self.observer = ClaudeCodeObserver(root=root)

    def plan_launch(
        self,
        *,
        participant_id: str,
        prompt: str,
        config_path: Path,
        approval: str,
        model: str | None = None,
        reasoning_effort: str | None = None,
        resume: str | None = None,
    ) -> LaunchPlan:
        if approval not in APPROVALS:
            raise BadRequest(f"approval must be one of {', '.join(APPROVALS)}, got {approval!r}")
        config = {
            "mcpServers": {
                SERVER_NAME: {
                    "command": theater_binary(),
                    "args": ["mcp", "--id", participant_id],
                }
            }
        }
        settings_path = _claude_settings_path(participant_id)
        token_path = paths.observation_dir("claude", participant_id) / "receipt-token"
        # `--mcp-config` is variadic in 2.x, so use `=` beside the prompt.
        argv = ["claude", f"--mcp-config={config_path}", f"--settings={settings_path}"]
        # Choose the id before the pane exists to remove the same-cwd creation race.
        native_session_id = str(uuid.uuid4())
        argv.append(f"--session-id={native_session_id}")
        if model:
            argv.append(f"--model={model}")
        if reasoning_effort:
            argv.append(f"--effort={reasoning_effort}")
        if resume:
            argv += [f"--resume={resume}", "--fork-session"]
        if approval == "yolo":
            argv.append("--dangerously-skip-permissions")
        elif approval == "edits":
            argv += ["--permission-mode", "acceptEdits"]
        if prompt:
            argv.append(prompt)
        return LaunchPlan(
            argv=argv,
            files={
                config_path: json.dumps(config, indent=2) + "\n",
                settings_path: json.dumps(
                    _claude_receipt_settings(participant_id, token_path), indent=2
                )
                + "\n",
            },
            private_files={},
            session_id=native_session_id,
            receipt_token_path=token_path,
        )

    def resume_launch_overlay(
        self,
        *,
        predecessor: Participant,
        trusted_session_owners: Sequence[Participant],
    ) -> ResumeLaunchOverlay:
        if predecessor.transcript_domain is None:
            return ResumeLaunchOverlay()
        root = cast("ClaudeCodeObserver", self.observer).root.resolve()
        return root_domain_overlay(
            predecessor, str(root), "Claude", resolve_declared=True, noun="root"
        )
