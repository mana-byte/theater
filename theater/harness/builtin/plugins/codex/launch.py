"""Codex launch, resume, and model discovery."""

from __future__ import annotations

from pathlib import Path

from theater.harness.base import (
    LaunchPlan,
    ResumeLaunchOverlay,
)
from theater.harness.contracts.callbacks import LaunchContext, ResumeContext
from theater.harness.transcript.discovery import root_domain_overlay

from .observer import CodexObserver


def plan_launch(context: LaunchContext) -> LaunchPlan:
    argv = ["codex"]
    if context.resume is not None:
        argv.append("fork")
        argv.append(context.resume)
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
