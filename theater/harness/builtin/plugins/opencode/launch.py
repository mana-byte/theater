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
from theater.models import BadRequest

from . import ids_v2
from .constants import (
    _APPROVAL_SESSION_RULES,
    MODELS_TIMEOUT,
    TUI_CONFIG_ENV_V2,
    TUI_PROMPTED_PERMISSIONS_V2,
)
from .dialect import (
    OpenCodeDialect,
    domain_for,
    installed_dialect,
    v2_database_for_domain,
    v2_database_path,
    v2_lineage_marker,
)
from .mcp import plugin_path
from .native_plugin import render_native_plugin
from .native_plugin_v2 import plugin_dir, render_native_plugin_v2
from .observer import database_path

#: `$1` is the create payload and the rest the TUI argv; the private server inherits the env.
_CREATE_THEN_EXEC = (
    'opencode api --standalone POST /api/session -d "$1" >/dev/null\nshift\nexec "$@"\n'
)


def plan_launch(
    context: LaunchContext,
    *,
    db: Path | None = None,
    dialect: OpenCodeDialect | None = None,
) -> LaunchPlan:
    if (dialect or installed_dialect()) is OpenCodeDialect.V2:
        return _plan_launch_v2(context)
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
    if context.model:
        argv += ["--model", context.model]
    # The plugin appends the approval ruleset to the session permission, merged after
    # agent rules; OPENCODE_PERMISSION lands in the global layer agents override.
    session_rules = _APPROVAL_SESSION_RULES.get(context.approval, ())
    if context.approval == "yolo":
        argv.append("--auto")
    if context.resume is not None:
        argv += ["-s", context.resume, "--fork"]
    elif context.prompt:
        argv += ["--prompt", context.prompt]
    files = {
        config_path: json.dumps(config, indent=2),
        native_plugin_path: render_native_plugin(participant_id, token_path, session_rules),
    }
    env = {
        "OPENCODE_CONFIG": str(config_path),
        "OPENCODE_DB": str(database),
    }
    return LaunchPlan(
        argv=argv,
        env=env,
        files=files,
        receipt_token_path=token_path,
    )


def _plan_launch_v2(context: LaunchContext) -> LaunchPlan:
    """A private `--standalone` server: the shared service would ignore this launch's env.

    2.x has no root `--model` or `--fork`: the model rides the config, and resume continues the
    session inside its own lineage database, whose id is therefore known before launch.
    """
    participant_id = context.participant_id
    config_path = context.config_path
    database = v2_database_path(participant_id)
    token_path = paths.participant_observation_dir(participant_id, "opencode") / "receipt-token"
    config: dict[str, object] = {
        "$schema": "https://opencode.ai/config.json",
        "plugin": [plugin_dir(config_path).resolve().as_uri()],
    }
    if context.model:
        config["model"] = context.model
    argv = ["opencode", "--standalone"]
    if context.approval == "yolo":
        argv.append("--auto")
    if context.resume is not None:
        argv += ["-s", context.resume]
    elif context.prompt:
        argv = _open_new_session_v2([*argv, "--prompt", context.prompt])
    marker, marker_text = v2_lineage_marker(participant_id)
    files = {
        config_path: json.dumps(config, indent=2),
        marker: marker_text,
        **render_native_plugin_v2(participant_id, config_path, token_path, context.approval),
    }
    env = {"OPENCODE_CONFIG": str(config_path), "OPENCODE_DB": str(database)}
    env.update(tui_env_v2(context.approval))
    return LaunchPlan(
        argv=argv,
        env=env,
        files=files,
        receipt_token_path=token_path,
        session_id=context.resume,
        transcript_domain=domain_for(database),
    )


def _open_new_session_v2(tui: list[str]) -> list[str]:
    """Create the session, then open the TUI on it: 2.0.18's home screen submits `--prompt`
    before a cold server lists models and never retries, while a session's screen waits.
    A failed create still opens the TUI, which then reports the missing session.
    """
    session = ids_v2.session_id()
    payload = json.dumps({"id": session})
    return ["/bin/sh", "-c", _CREATE_THEN_EXEC, "theater-opencode", payload, *tui, "-s", session]


def tui_env_v2(approval: str | None) -> dict[str, str]:
    """Yolo keeps `--auto`; any other approval must not inherit a client-side autoaccept."""
    return {} if approval == "yolo" else {TUI_CONFIG_ENV_V2: TUI_PROMPTED_PERMISSIONS_V2}


def resume_launch_overlay(
    context: ResumeContext,
    *,
    db: Path | None = None,
    dialect: OpenCodeDialect | None = None,
) -> ResumeLaunchOverlay:
    predecessor = context.predecessor
    lineage = v2_database_for_domain(predecessor.transcript_domain)
    if (dialect or installed_dialect()) is OpenCodeDialect.V2:
        return _resume_overlay_v2(context, lineage)
    if lineage is not None:
        raise BadRequest(
            "cannot resume this OpenCode session: it was recorded by OpenCode 2.x, and the "
            "opencode on PATH is 1.x. Put OpenCode 2.x first on PATH to resume it, or start a "
            "new session."
        )
    if predecessor.transcript_domain is None:
        return ResumeLaunchOverlay()
    expected = f"opencode://{database_path(db)}"
    return root_domain_overlay(predecessor, expected, "OpenCode")


def _resume_overlay_v2(context: ResumeContext, lineage: Path | None) -> ResumeLaunchOverlay:
    """Continue the session in its own lineage database, never in one 1.x also uses."""
    if lineage is None:
        raise BadRequest(
            "cannot resume this OpenCode session with OpenCode 2.x: it was recorded by OpenCode "
            "1.x, and 2.x migrates (and clears the event log of) any 1.x database it opens. "
            "Put OpenCode 1.x first on PATH to resume it, or start a new session."
        )
    domain = domain_for(lineage)
    inside = lineage.expanduser().resolve().is_relative_to(paths.participants_dir().resolve())
    trusted = any(owner.transcript_domain == domain for owner in context.trusted_session_owners)
    if not inside or not trusted:
        raise BadRequest(
            "cannot resume OpenCode session safely: its database is not a Theater-isolated 2.x "
            "lineage of a trusted predecessor. Start a new session instead."
        )
    if not lineage.exists():
        raise BadRequest(
            f"cannot resume OpenCode session: its database {str(lineage)!r} no longer exists. "
            "Start a new session instead."
        )
    return ResumeLaunchOverlay(env={"OPENCODE_DB": str(lineage)}, transcript_domain=domain)


def discover_models(context: ModelDiscoveryContext) -> Sequence[str]:
    # 2.x answers through its background service: a cold `--standalone` server lists no models
    # yet, and only the TUI (never `models`) replaces a service of another version.
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
