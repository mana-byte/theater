"""OpenCode launch, resume, and model discovery."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from pathlib import Path

from theater import paths
from theater.harness.contracts.callbacks import (
    LaunchContext,
    ModelDiscoveryContext,
    ResumeContext,
)
from theater.harness.contracts.launch import LaunchPlan, ResumeLaunchOverlay
from theater.harness.transcript.discovery import root_domain_overlay

from .constants import _APPROVAL_PERMISSIONS, MODELS_TIMEOUT
from .mcp import plugin_path
from .native_plugin import render_native_plugin
from .observer import database_path


def plan_launch(context: LaunchContext, *, db: Path | None = None) -> LaunchPlan:
    participant_id = context.participant_id
    config_path = context.config_path
    database = database_path(db)
    config: dict[str, object] = {
        "$schema": "https://opencode.ai/config.json",
    }
    native_plugin_path = plugin_path(config_path)
    token_path = paths.participant_observation_dir(participant_id, "opencode") / "receipt-token"
    config["plugin"] = [native_plugin_path.resolve().as_uri()]
    argv = ["opencode"]
    permission_env: dict[str, str] = {}
    if context.model:
        argv += ["--model", context.model]
    if context.approval == "yolo":
        argv.append("--auto")
    elif context.approval in _APPROVAL_PERMISSIONS:
        # Native's build agent merges `"*": "allow"` permission defaults with
        # every config file, so manual/edits must be enforced through
        # OPENCODE_PERMISSION — the one permission layer merged after all of
        # them, including project-local config.
        permission_env["OPENCODE_PERMISSION"] = json.dumps(_APPROVAL_PERMISSIONS[context.approval])
    if context.resume is not None:
        argv += ["-s", context.resume, "--fork"]
    elif context.prompt:
        argv += ["--prompt", context.prompt]
    files = {
        config_path: json.dumps(config, indent=2),
        native_plugin_path: render_native_plugin(participant_id, token_path),
    }
    env = {
        "OPENCODE_CONFIG": str(config_path),
        "OPENCODE_DB": str(database),
    }
    env.update(permission_env)
    return LaunchPlan(
        argv=argv,
        env=env,
        files=files,
        receipt_token_path=token_path,
    )


def resume_launch_overlay(context: ResumeContext, *, db: Path | None = None) -> ResumeLaunchOverlay:
    if context.predecessor.transcript_domain is None:
        return ResumeLaunchOverlay()
    expected = f"opencode://{database_path(db)}"
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
