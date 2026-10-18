"""Codex native runtime planning: compatibility probe and pure plans.

Auto selects native only inside the qualified release range (floor, ceiling, excluded);
everything else selects legacy, with the reason recorded for both auto and explicit native
preference (the daemon treats native as a preference, never a hard requirement).
"""

from __future__ import annotations

import re
import subprocess

from theater.harness.contracts.launch import LaunchPlan
from theater.harness.contracts.runtime import (
    RuntimeCompatibility,
    RuntimePlan,
    RuntimePlanningContext,
    RuntimeProbeContext,
)

from .constants import CODEX_BINARY

#: First codex-cli release proved compatible; the native runtime is untested below it.
CODEX_RUNTIME_QUALIFIED_FLOOR = (0, 148, 0)
#: First release not yet proved; the daily compat pipeline moves it over an unbroken run of greens.
CODEX_RUNTIME_QUALIFIED_CEILING = (0, 163, 1)
#: Releases inside the range measured incompatible; each entry needs a comment naming the cause.
CODEX_RUNTIME_QUALIFIED_EXCLUDED: tuple[tuple[int, int, int], ...] = ()


def _release_text(release: tuple[int, int, int]) -> str:
    return ".".join(str(part) for part in release)


#: Tested compatibility policy; derived from the floor so a ceiling bump never renames it.
CODEX_RUNTIME_COMPATIBILITY_POLICY = (
    f"codex-appserver-{_release_text(CODEX_RUNTIME_QUALIFIED_FLOOR)}-compatible"
)

#: ``codex --version`` prints ``codex-cli <version>`` on the verified release.
CODEX_RUNTIME_VERSION_PREFIX = "codex-cli"

#: Deadline for the read-only version probe.
CODEX_RUNTIME_PROBE_TIMEOUT_SECONDS = 10.0

_VERSION_TOKEN = re.compile(r"codex-cli[ \t]+(\S+)")
_STABLE_RELEASE = re.compile(r"([0-9]+)\.([0-9]+)\.([0-9]+)")

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
    """Display text of the qualified range, derived from the gate constants."""
    text = (
        f">={_release_text(CODEX_RUNTIME_QUALIFIED_FLOOR)},"
        f"<{_release_text(CODEX_RUNTIME_QUALIFIED_CEILING)}"
    )
    if CODEX_RUNTIME_QUALIFIED_EXCLUDED:
        text += " excluding " + ", ".join(map(_release_text, CODEX_RUNTIME_QUALIFIED_EXCLUDED))
    return text


def codex_version_refusal(version: str) -> str | None:
    """Why this release is outside the qualified range, or None when it is admitted."""
    match = _STABLE_RELEASE.fullmatch(version)
    if match is None:
        return f"codex-cli {version!r} is not a stable MAJOR.MINOR.PATCH release"
    release = (int(match[1]), int(match[2]), int(match[3]))
    if release in CODEX_RUNTIME_QUALIFIED_EXCLUDED:
        return f"codex-cli {version} was measured incompatible and is excluded"
    if release < CODEX_RUNTIME_QUALIFIED_FLOOR:
        return f"codex-cli {version} is older than the qualified floor"
    if release >= CODEX_RUNTIME_QUALIFIED_CEILING:
        return f"codex-cli {version} is newer than the newest proved release"
    return None


def codex_refusal_message(version: str) -> str | None:
    """The actionable refusal for an unqualified release; wiring=auto falls back to legacy."""
    refusal = codex_version_refusal(version)
    if refusal is None:
        return None
    return (
        f"{refusal} (compatibility policy {CODEX_RUNTIME_COMPATIBILITY_POLICY}, qualified range "
        f"{qualified_versions_range_text()}); wiring=auto selects legacy, and explicit native "
        "preference selects legacy with this reason"
    )


def codex_endpoint_url(endpoint: str) -> str:
    """The app-server listen/remote URL for one private local endpoint (bare path or schemed)."""
    if "://" in endpoint:
        return endpoint
    return f"unix://{endpoint}"


def parse_codex_version(output: str) -> str | None:
    """Extract the exact installed codex-cli version from ``--version`` output."""
    match = _VERSION_TOKEN.search(output)
    if match is None:
        return None
    return match.group(1)


def probe_codex_compatibility(context: RuntimeProbeContext) -> RuntimeCompatibility:
    """Read-only probe of installed codex-cli against the qualified release range."""
    binary = context.binary or CODEX_BINARY
    version: str | None = None
    try:
        completed = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            text=True,
            timeout=CODEX_RUNTIME_PROBE_TIMEOUT_SECONDS,
            check=False,
        )
        output = f"{completed.stdout}\n{completed.stderr}"
    except (OSError, subprocess.SubprocessError) as error:
        return RuntimeCompatibility(
            supported=False,
            policy=CODEX_RUNTIME_COMPATIBILITY_POLICY,
            native_version=None,
            reason=(
                f"codex compatibility probe could not run {binary!r} --version: {error}; "
                "wiring=auto and explicit native preference both select legacy with this reason"
            ),
        )
    if completed.returncode != 0:
        return RuntimeCompatibility(
            supported=False,
            policy=CODEX_RUNTIME_COMPATIBILITY_POLICY,
            native_version=None,
            reason=(
                f"codex --version exited with {completed.returncode}; wiring=auto selects "
                "legacy, and explicit native preference selects legacy with this reason"
            ),
        )
    version = parse_codex_version(output)
    if version is None:
        return RuntimeCompatibility(
            supported=False,
            policy=CODEX_RUNTIME_COMPATIBILITY_POLICY,
            native_version=None,
            reason=(
                f"codex --version output did not name a codex-cli version: {output.strip()!r}; "
                "wiring=auto and explicit native preference both select legacy with this reason"
            ),
        )
    refusal = codex_refusal_message(version)
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
    argv: list[str] = [CODEX_BINARY]
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
) -> LaunchPlan:
    """Plan a promptless native CLI UI attachment."""
    # A promptless TUI shows a blocking update dialog before it creates its thread, and
    # nobody is there to answer it; only a CLI prompt skips it.
    argv = [CODEX_BINARY, "-c", "check_for_update_on_startup=false"]
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
