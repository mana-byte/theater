"""Codex launch, resume, and model discovery."""

from __future__ import annotations

from pathlib import Path

from theater.harness.base import (
    LaunchPlan,
    ResumeLaunchOverlay,
)
from theater.harness.contracts.callbacks import LaunchContext, ResumeContext
from theater.harness.transcript.discovery import root_domain_overlay
from theater.models import BadRequest

from .homes import codex_home, home_for_launch, sessions_root


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
        # `-a untrusted` was removed from the codex CLI (only `on-request` and
        # `never` remain); `on-request` is the default policy on every codex
        # release, so it is the backward-compatible value here.
        argv += ["-a", "on-request", "-s", "read-only"]
    if context.prompt:
        argv.append(context.prompt)
    # Pin CODEX_HOME so the child's rollout root is exactly the one observation
    # derives; unset/empty pins "" (Codex's default home), never a path the
    # child would then require to exist.
    env = {"CODEX_HOME": home_for_launch()}
    return LaunchPlan(argv=argv, env=env)


def resume_launch_overlay(
    context: ResumeContext, *, root: Path | None = None
) -> ResumeLaunchOverlay:
    predecessor = context.predecessor
    if root is None:
        home = codex_home()
        if home is not None and not home.is_absolute():
            # A forked pane may run in another cwd, so a relative home would
            # move the session off the predecessor's home entirely.
            base = Path(predecessor.cwd) if predecessor.cwd else Path.cwd()
            absolute = (base / home).resolve()
            raise BadRequest(
                f"cannot resume Codex session: CODEX_HOME {str(home)!r} is relative and the "
                "resumed pane may run in a different working directory, so the fork would "
                f"not see the predecessor's home; set CODEX_HOME to its absolute equivalent "
                f"{str(absolute)!r} before resuming"
            )
    if predecessor.transcript_domain is None:
        return ResumeLaunchOverlay()
    resolved_root = (root or sessions_root(cwd=predecessor.cwd)).resolve()
    return root_domain_overlay(
        predecessor, str(resolved_root), "Codex", resolve_declared=True, noun="root"
    )
