"""Vibe launch, resume, and model discovery."""

from __future__ import annotations

import json
import os
import tomllib
from collections.abc import Sequence
from pathlib import Path

from theater.harness.base import (
    MCP_TOOL_TIMEOUT,
    SERVER_NAME,
    LaunchPlan,
    ResumeLaunchOverlay,
    theater_binary,
)
from theater.harness.contracts.callbacks import (
    LaunchContext,
    ModelDiscoveryContext,
    ResumeContext,
)
from theater.models import BadRequest

from .constants import (
    ISOLATION_MARKER,
    VIBE_ACTIVE_MODEL_ENV,
    VIBE_CONFIG_FILENAME,
    VIBE_HOME_ENV,
    VIBE_MCP_SERVERS_ENV,
    VIBE_SESSION_LOGGING_SAVE_DIR_ENV,
    VIBE_WAIT_MCP_SERVER_NAME,
)
from .identity import participant_root
from .isolation import _canonical, isolation_marker_text, validate_isolated_domain


def plan_launch(
    context: LaunchContext,
    *,
    correlation_root: Path | None = None,
) -> LaunchPlan:
    participant_id = context.participant_id
    prompt = context.prompt
    approval = context.approval
    model = context.model
    resume = context.resume
    servers = [
        {
            "name": SERVER_NAME,
            "transport": "stdio",
            "command": theater_binary(),
            "args": ["mcp", "--id", participant_id, "--harness", "vibe", "--toolset", "control"],
            # Vibe's 60s default cuts off `await_sessions` before the daemon's 300s ceiling.
            "tool_timeout_sec": MCP_TOOL_TIMEOUT,
        },
        {
            # Separate config keeps cancelled waits from blocking control RPCs.
            "name": VIBE_WAIT_MCP_SERVER_NAME,
            "transport": "stdio",
            "command": theater_binary(),
            "args": ["mcp", "--id", participant_id, "--harness", "vibe", "--toolset", "wait"],
            "tool_timeout_sec": MCP_TOOL_TIMEOUT,
        },
    ]
    argv = ["vibe"]
    if approval == "yolo":
        argv.append("--yolo")
    elif approval == "edits":
        argv += ["--agent", "accept-edits"]
    # --resume appends to the same messages.jsonl, keeps the session id; prompt still honoured.
    if resume is not None:
        argv += ["--resume", resume]
    if prompt:
        argv.append(prompt)
    env = {VIBE_MCP_SERVERS_ENV: json.dumps(servers)}
    # No `--model` flag: the same VIBE_* override carries the model. Empty = configured default.
    env[VIBE_ACTIVE_MODEL_ENV] = model or ""
    files: dict[Path, str] = {}
    transcript_domain: Path | None = None
    if resume is None:
        # Vibe's env uses `__` for nested fields. All sessions land under one root.
        save_dir = participant_root(participant_id, correlation_root)
        env[VIBE_SESSION_LOGGING_SAVE_DIR_ENV] = str(save_dir)
        files[save_dir / ISOLATION_MARKER] = isolation_marker_text(
            participant_id=participant_id,
            transcript_domain=save_dir,
        )
        transcript_domain = _canonical(save_dir)
    return LaunchPlan(
        argv=argv,
        env=env,
        files=files,
        session_id=resume,
        transcript_domain=str(transcript_domain) if transcript_domain is not None else None,
    )


def resume_launch_overlay(context: ResumeContext) -> ResumeLaunchOverlay:
    """Validate and reuse a trusted predecessor's isolated transcript domain."""
    predecessor = context.predecessor
    if predecessor.transcript_domain is None:
        raise BadRequest(
            "cannot resume Vibe session safely: predecessor has no isolated "
            "transcript domain. Rebind or migrate the session into a Theater "
            "isolated Vibe domain, then retry."
        )
    domain = Path(predecessor.transcript_domain).expanduser().resolve(strict=False)
    marker = validate_isolated_domain(domain)
    if marker is None:
        raise BadRequest(
            "cannot resume Vibe session safely: predecessor uses a legacy or "
            "untrusted transcript root. Rebind or migrate it into a Theater "
            "isolated Vibe domain, then retry."
        )
    marker_owner = marker.get("participant_id")
    if not isinstance(marker_owner, str) or not _domain_owner_in_trusted_set(
        owner_id=marker_owner,
        domain=domain,
        trusted_owners=context.trusted_session_owners,
    ):
        raise BadRequest(
            "cannot resume Vibe session safely: isolated transcript domain "
            "belongs to a different Theater session lineage. Rebind or "
            "migrate the session into its own isolated Vibe domain, then retry."
        )
    if predecessor.transcript_location is not None:
        location = Path(predecessor.transcript_location)
        try:
            location.resolve().relative_to(domain)
        except (OSError, ValueError) as exc:
            raise BadRequest(
                "cannot resume Vibe session safely: predecessor transcript "
                "location is outside its isolated transcript domain"
            ) from exc
    return ResumeLaunchOverlay(
        env={VIBE_SESSION_LOGGING_SAVE_DIR_ENV: str(domain)},
        transcript_domain=str(domain),
    )


def _domain_owner_in_trusted_set(*, owner_id: str, domain: Path, trusted_owners: tuple) -> bool:
    """Whether the signed domain owner anchors a trusted resume chain."""
    return any(
        p.id == owner_id
        and p.transcript_domain is not None
        and Path(p.transcript_domain).expanduser().resolve(strict=False) == domain
        for p in trusted_owners
    )


def discover_models(context: ModelDiscoveryContext) -> Sequence[str]:
    """Read Vibe model names and aliases from its config."""
    del context
    path = _config_path()
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise NotImplementedError(f"{path} does not exist") from exc
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise NotImplementedError(f"{path} cannot be read: {exc}") from exc

    entries = raw.get("models")
    if not isinstance(entries, list):
        raise NotImplementedError(f"{path} has no [[models]] entries")

    found: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        for key in ("name", "alias"):
            value = entry.get(key)
            if isinstance(value, str) and value and value not in found:
                found.append(value)
    return found


def _config_path() -> Path:
    """Where vibe keeps its config. `$VIBE_HOME` wins, as it does for vibe."""
    home = os.environ.get(VIBE_HOME_ENV)
    base = Path(home) if home else Path.home() / ".vibe"
    return base / VIBE_CONFIG_FILENAME
