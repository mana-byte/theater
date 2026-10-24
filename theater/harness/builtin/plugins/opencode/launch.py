"""OpenCode launch, resume, and model discovery."""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from theater import paths
from theater.constants.harness import HARNESS_APPROVAL_POLICIES
from theater.harness.contracts.callbacks import (
    LaunchContext,
    ModelDiscoveryContext,
    ResumeContext,
)
from theater.harness.contracts.launch import LaunchPlan, ResumeLaunchOverlay
from theater.harness.normalization import literal_prompt_argument
from theater.models import BadRequest

from . import ids
from .approval import approval_ruleset
from .constants import (
    BOOTSTRAP_CREDENTIAL_NAME,
    MODELS_TIMEOUT,
    TUI_CONFIG_ENV,
    TUI_PROMPTED_PERMISSIONS,
)
from .dialect import (
    database_for_domain,
    domain_for,
    lineage_marker,
    participant_database_path,
    require_supported_version,
    resolve_binary,
)
from .legacy_bootstrap import bootstrap_path, render_legacy_bootstrap
from .native_plugin import plugin_dir, render_native_plugin

#: `$0` is the resolved binary, `$1` the create payload, the rest — after one
#: `shift` — the full TUI argv, binary included. A failed create aborts pre-TUI.
_CREATE_THEN_EXEC = (
    '"$0" api --standalone POST /api/session -d "$1" >/dev/null || exit 1\nshift\nexec "$@"\n'
)


def plan_launch(
    context: LaunchContext,
) -> LaunchPlan:
    binary = resolve_binary(context.binary or "opencode")
    require_supported_version(binary)
    return _plan_launch(context, binary=binary)


def _plan_launch(context: LaunchContext, *, binary: str) -> LaunchPlan:
    """A private `--standalone` server: the shared service would ignore this launch's env.

    2.x has no root `--model` or `--fork`: the model rides the config, and resume continues the
    session inside its own lineage database, whose id is therefore known before launch.
    `manual`/`edits` route through the fail-closed bootstrap, which owns one private `serve`,
    verifies the approval plugin on it, protects the session, and only then opens the TUI.
    """
    if context.approval not in HARNESS_APPROVAL_POLICIES:
        raise BadRequest(
            f"unknown approval {context.approval!r}; expected one of "
            f"{', '.join(HARNESS_APPROVAL_POLICIES)}"
        )
    participant_id = context.participant_id
    config_path = context.config_path
    database = participant_database_path(participant_id)
    token_path = paths.participant_observation_dir(participant_id, "opencode") / "receipt-token"
    config: dict[str, object] = {
        "$schema": "https://opencode.ai/config.json",
        "plugin": [plugin_dir(config_path).resolve().as_uri()],
    }
    if context.model:
        config["model"] = context.model
    marker, marker_text = lineage_marker(participant_id)
    files = {
        config_path: json.dumps(config, indent=2),
        marker: marker_text,
        **render_native_plugin(participant_id, config_path, token_path, context.approval),
    }
    env = {"OPENCODE_CONFIG": str(config_path), "OPENCODE_DB": str(database)}
    if approval_ruleset(context.approval):
        return _plan_enforced_launch(context, binary=binary, files=files, env=env)
    argv = [binary, "--standalone"]
    if context.approval == "yolo":
        argv.append("--auto")
    if context.resume is not None:
        argv += ["-s", context.resume]
    elif context.prompt:
        argv = _open_new_session(
            binary, [*argv, "--prompt", literal_prompt_argument(context.prompt)]
        )
    env.update(tui_env(context.approval))
    return LaunchPlan(
        argv=argv,
        env=env,
        files=files,
        receipt_token_path=token_path,
        session_id=context.resume,
        transcript_domain=domain_for(database),
    )


def _plan_enforced_launch(
    context: LaunchContext, *, binary: str, files: dict[Path, str], env: dict[str, str]
) -> LaunchPlan:
    """Verify the approval plugin on a private server before opening its TUI."""
    participant_id = context.participant_id
    config_path = context.config_path
    database = participant_database_path(participant_id)
    observation = paths.participant_observation_dir(participant_id, "opencode")
    token_path = observation / "receipt-token"
    session_id = context.resume or ids.session_id()
    # The bootstrap appends ["--prompt", settings["prompt"]] to the TUI argv,
    # so the stored prompt must already be the literal-safe form.
    prompt = (
        None if context.resume or not context.prompt else literal_prompt_argument(context.prompt)
    )
    settings = {
        "binary": binary,
        "approval": context.approval,
        "config": str(config_path),
        "database": str(database),
        "credential": str(observation / BOOTSTRAP_CREDENTIAL_NAME),
        "plugin_source": str((plugin_dir(config_path) / "server.js").resolve()),
        "session_id": None if context.resume else session_id,
        "resume": context.resume,
        "prompt": prompt,
        "model": context.model,
        "tui_env": tui_env(context.approval),
    }
    files[bootstrap_path(config_path)] = render_legacy_bootstrap(settings)
    return LaunchPlan(
        argv=[sys.executable, str(bootstrap_path(config_path))],
        env=env,
        files=files,
        receipt_token_path=token_path,
        session_id=session_id,
        transcript_domain=domain_for(database),
    )


def _open_new_session(binary: str, tui: list[str]) -> list[str]:
    """Create the session, then open the TUI on it: 2.0.18's home screen submits `--prompt`
    before a cold server lists models and never retries, while a session's screen waits.
    A failed create aborts before the TUI opens, so no prompt is lost.
    """
    session = ids.session_id()
    payload = json.dumps({"id": session})
    return ["/bin/sh", "-c", _CREATE_THEN_EXEC, binary, payload, *tui, "-s", session]


def tui_env(approval: str | None) -> dict[str, str]:
    """Yolo keeps `--auto`; any other approval must not inherit a client-side autoaccept."""
    return {} if approval == "yolo" else {TUI_CONFIG_ENV: TUI_PROMPTED_PERMISSIONS}


def resume_launch_overlay(
    context: ResumeContext,
) -> ResumeLaunchOverlay:
    lineage = database_for_domain(context.predecessor.transcript_domain)
    return _resume_overlay(context, lineage)


def _resume_overlay(context: ResumeContext, lineage: Path | None) -> ResumeLaunchOverlay:
    """Continue only an authenticated, isolated database lineage."""
    if lineage is None:
        raise BadRequest(
            "cannot resume this OpenCode session: it has no supported isolated database "
            "lineage. Start a new session instead."
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
