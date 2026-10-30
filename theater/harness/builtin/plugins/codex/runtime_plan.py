"""Codex native runtime planning: compatibility probe and pure plans.

Native and provider launches share the plugin's qualified stable release policy.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from theater.harness.contracts import version_probe
from theater.harness.contracts.launch import LaunchPlan
from theater.harness.contracts.runtime import (
    RuntimeCompatibility,
    RuntimePlan,
    RuntimePlanningContext,
    RuntimeProbeContext,
)
from theater.harness.contracts.version_policy import VersionPolicy

from .constants import CODEX_BINARY

VERSION_POLICY = VersionPolicy.load(Path(__file__).with_name("compatibility.json"))
CODEX_RUNTIME_COMPATIBILITY_POLICY = f"codex-appserver-{VERSION_POLICY.minimum}-compatible"

#: Deadline for the read-only version probe.
CODEX_RUNTIME_PROBE_TIMEOUT_SECONDS = 10.0

_VERSION_TOKEN = re.compile(r"codex-cli[ \t]+(\S+)")

#: Backend config overrides per approval mode. Approval is explicit per
#: spawn — it has no default anywhere: a missing or unknown approval mode is
#: rejected, never silently mapped to a fallback.
_CODEX_APPROVAL_OVERRIDES: dict[str, tuple[tuple[str, str], ...]] = {
    "yolo": (
        ("approval_policy", "never"),
        ("sandbox_mode", "danger-full-access"),
    ),
    "edits": (
        ("approval_policy", "on-request"),
        ("sandbox_mode", "workspace-write"),
    ),
    # `-a untrusted` was removed from the codex CLI; `on-request` is the
    # default policy on every codex release, matching the legacy launch plan.
    "manual": (
        ("approval_policy", "on-request"),
        ("sandbox_mode", "read-only"),
    ),
}


def qualified_versions_range_text() -> str:
    """Display the same inclusive policy used for admission."""
    return VERSION_POLICY.range_text


def codex_endpoint_url(endpoint: str) -> str:
    """The app-server listen/remote URL for one private local endpoint (bare path or schemed)."""
    if "://" in endpoint:
        return endpoint
    return f"unix://{endpoint}"


def probe_codex_compatibility(context: RuntimeProbeContext) -> RuntimeCompatibility:
    """Read-only probe of installed codex-cli against the qualified release range."""
    binary = context.binary or CODEX_BINARY
    version: str | None = None
    try:
        version = version_probe.probe_release(
            binary, _VERSION_TOKEN, timeout=CODEX_RUNTIME_PROBE_TIMEOUT_SECONDS
        )
    except subprocess.CalledProcessError as error:
        return RuntimeCompatibility(
            supported=False,
            policy=CODEX_RUNTIME_COMPATIBILITY_POLICY,
            reason=(
                f"codex --version exited with {error.returncode}; "
                "refusing an unverified CLI release"
            ),
        )
    except (OSError, subprocess.SubprocessError) as error:
        return RuntimeCompatibility(
            supported=False,
            policy=CODEX_RUNTIME_COMPATIBILITY_POLICY,
            native_version=None,
            reason=(
                f"codex compatibility probe could not run {binary!r} --version: {error}; "
                "refusing an unverified CLI release"
            ),
        )
    if version is None:
        return RuntimeCompatibility(
            supported=False,
            policy=CODEX_RUNTIME_COMPATIBILITY_POLICY,
            native_version=None,
            reason=(
                "codex --version output did not name a codex-cli version; "
                "refusing an unverified CLI release"
            ),
        )
    refusal = VERSION_POLICY.refusal("codex-cli", version)
    if refusal is not None:
        return RuntimeCompatibility(
            supported=False,
            policy=CODEX_RUNTIME_COMPATIBILITY_POLICY,
            native_version=version,
            reason=refusal,
        )
    return RuntimeCompatibility(
        supported=True,
        policy=CODEX_RUNTIME_COMPATIBILITY_POLICY,
        native_version=version,
    )


def codex_backend_config_overrides(
    context: RuntimePlanningContext,
) -> tuple[tuple[str, str], ...]:
    """Return the participant's backend ``-c`` overrides."""
    return codex_launch_config_overrides(
        approval=context.approval,
        model=context.model,
        reasoning_effort=context.reasoning_effort,
    )


def codex_launch_config_overrides(
    *,
    approval: str | None,
    model: str | None = None,
    reasoning_effort: str | None = None,
) -> tuple[tuple[str, str], ...]:
    """Return explicit per-spawn Codex configuration."""
    overrides = _CODEX_APPROVAL_OVERRIDES.get(approval) if approval is not None else None
    if overrides is None:
        known = ", ".join(sorted(_CODEX_APPROVAL_OVERRIDES))
        raise ValueError(
            "approval mode must be explicit per spawn and one of "
            f"({known}); got {approval!r} — approval has no default"
        )
    pairs = list(overrides)
    if model:
        pairs.append(("model", model))
    if reasoning_effort:
        pairs.append(("model_reasoning_effort", reasoning_effort))
    return tuple(pairs)


def codex_thread_config_overrides(
    *,
    approval: str | None,
    model: str | None = None,
    reasoning_effort: str | None = None,
) -> dict[str, object]:
    """Return typed app-server overrides for a new or forked thread."""
    config = dict(
        codex_launch_config_overrides(
            approval=approval,
            model=model,
            reasoning_effort=reasoning_effort,
        )
    )
    params: dict[str, object] = {
        "approvalPolicy": config.pop("approval_policy"),
        "sandbox": config.pop("sandbox_mode"),
    }
    selected_model = config.pop("model", None)
    if selected_model is not None:
        params["model"] = selected_model
    if config:
        params["config"] = config
    return params


def plan_codex_runtime_backend(context: RuntimePlanningContext) -> RuntimePlan:
    """Pure plan for one participant's detached Codex app-server backend.

    ``--listen unix://`` speaks WebSocket, not Theater NDJSON; the daemon adds MCP overrides
    and launches it in the worktree, so every thread captures that cwd.
    """
    argv: list[str] = [context.binary or CODEX_BINARY]
    for key, value in codex_backend_config_overrides(context):
        argv += ["-c", f"{key}={value}"]
    if context.endpoint is None:
        raise ValueError(
            "codex app-server requires the daemon-selected private socket endpoint; "
            "a null planning endpoint means stdout discovery, which codex does not declare"
        )
    argv += ["app-server", "--listen", codex_endpoint_url(context.endpoint)]
    return RuntimePlan(backend=LaunchPlan(argv=argv), endpoint=context.endpoint)


def plan_codex_frontend(
    endpoint: str,
    *,
    native_session_id: str | None,
    approval: str | None,
    model: str | None = None,
    reasoning_effort: str | None = None,
    binary: str | None = None,
) -> LaunchPlan:
    """Plan a promptless native CLI UI attachment."""
    # A promptless TUI shows a blocking update dialog before it creates its thread, and
    # nobody is there to answer it; only a CLI prompt skips it.
    argv = [binary or CODEX_BINARY, "-c", "check_for_update_on_startup=false"]
    for key, value in codex_launch_config_overrides(
        approval=approval,
        model=model,
        reasoning_effort=reasoning_effort,
    ):
        argv += ["-c", f"{key}={value}"]
    argv += ["--remote", codex_endpoint_url(endpoint)]
    if native_session_id is not None:
        argv += ["resume", native_session_id]
    return LaunchPlan(argv=argv)
