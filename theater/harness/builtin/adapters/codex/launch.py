"""Codex launch, resume, and model discovery."""

from __future__ import annotations

import json
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

from .constants import CODEX_BINARY

if TYPE_CHECKING:
    from theater.models import Participant


class CodexHarness(Harness):
    name = "codex"
    binary = CODEX_BINARY
    icon = "\u25c9"
    aliases = ("codex-cli", "codex_cli", "openai-codex", "Codex")
    resume_strategy = "fork"

    def __init__(self, root: Path | None = None):
        from .observer import CodexObserver

        self.observer = CodexObserver(root=root)

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
        command = json.dumps(theater_binary())
        args = json.dumps(["mcp", "--id", participant_id])
        argv = [
            "codex",
        ]
        if resume is not None:
            argv.append("fork")
            argv.append(resume)
        argv += [
            "-c",
            f"mcp_servers.{SERVER_NAME}.command={command}",
            "-c",
            f"mcp_servers.{SERVER_NAME}.args={args}",
        ]
        if model:
            argv += ["--model", model]
        if reasoning_effort:
            argv += ["-c", f"model_reasoning_effort={reasoning_effort}"]
        if approval == "yolo":
            argv.append("--dangerously-bypass-approvals-and-sandbox")
        elif approval == "edits":
            argv += ["-a", "on-request", "-s", "workspace-write"]
        else:
            argv += ["-a", "untrusted", "-s", "read-only"]
        if prompt:
            argv.append(prompt)
        return LaunchPlan(argv=argv)

    def resume_launch_overlay(
        self,
        *,
        predecessor: Participant,
        trusted_session_owners: Sequence[Participant],
    ) -> ResumeLaunchOverlay:
        if predecessor.transcript_domain is None:
            return ResumeLaunchOverlay()
        root = self.observer.root.resolve()  # type: ignore[attr-defined]
        declared = Path(predecessor.transcript_domain).resolve(strict=False)
        if declared != root:
            raise BadRequest(
                f"cannot resume Codex session: predecessor transcript domain "
                f"{declared!r} does not match the Codex observation root {root!r}"
            )
        return ResumeLaunchOverlay(transcript_domain=str(root))
