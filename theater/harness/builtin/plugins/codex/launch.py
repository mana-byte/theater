"""Codex launch, resume, and model discovery."""

from __future__ import annotations

import json
from pathlib import Path

from theater.harness.base import (
    SERVER_NAME,
    LaunchPlan,
    ResumeLaunchOverlay,
    theater_binary,
)
from theater.harness.contracts.callbacks import LaunchContext, ResumeContext
from theater.harness.transcript.discovery import root_domain_overlay

from .observer import CodexObserver

_WAIT_SERVER_NAME = f"{SERVER_NAME}_wait"


def plan_launch(context: LaunchContext) -> LaunchPlan:
    command = json.dumps(theater_binary())
    control_args = json.dumps(
        [
            "mcp",
            "--id",
            context.participant_id,
            "--harness",
            "codex",
            "--toolset",
            "control",
        ]
    )
    wait_args = json.dumps(
        [
            "mcp",
            "--id",
            context.participant_id,
            "--harness",
            "codex",
            "--toolset",
            "wait",
        ]
    )
    argv = ["codex"]
    if context.resume is not None:
        argv.append("fork")
        argv.append(context.resume)
    argv += [
        "-c",
        f"mcp_servers.{SERVER_NAME}.command={command}",
        "-c",
        f"mcp_servers.{SERVER_NAME}.args={control_args}",
        "-c",
        f"mcp_servers.{_WAIT_SERVER_NAME}.command={command}",
        "-c",
        f"mcp_servers.{_WAIT_SERVER_NAME}.args={wait_args}",
    ]
    if context.model:
        argv += ["--model", context.model]
    if context.reasoning_effort:
        argv += ["-c", f"model_reasoning_effort={context.reasoning_effort}"]
    if context.approval == "yolo":
        argv.append("--dangerously-bypass-approvals-and-sandbox")
    elif context.approval == "edits":
        argv += ["-a", "on-request", "-s", "workspace-write"]
    else:
        argv += ["-a", "untrusted", "-s", "read-only"]
    if context.prompt:
        argv.append(context.prompt)
    return LaunchPlan(argv=argv)


def resume_launch_overlay(
    context: ResumeContext, *, root: Path | None = None
) -> ResumeLaunchOverlay:
    predecessor = context.predecessor
    if predecessor.transcript_domain is None:
        return ResumeLaunchOverlay()
    resolved_root = (root or CodexObserver().root).resolve()
    return root_domain_overlay(
        predecessor, str(resolved_root), "Codex", resolve_declared=True, noun="root"
    )
