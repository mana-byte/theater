"""Codex native runtime planning: compatibility probe and pure plans.

Everything in this module is read-only or pure: the probe runs ``codex
--version`` and compares the installed release against the Theater-verified
compatibility policy (Wave 0 fixtures, ``tests/fixtures/codex_native_runtime``),
and the planners build launch plans without side effects. Automatic native
selection means *Theater-verified* compatibility, never presumed vendor
stability: an unknown or unsupported version selects legacy under
``wiring="auto"``; an explicit ``wiring="native"`` fails with the recorded
reason.
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

#: Name of the tested compatibility policy (Wave 0 proof, codex-cli 0.154.0).
CODEX_RUNTIME_COMPATIBILITY_POLICY = "codex-appserver-0.154-verified"

#: Exact codex-cli releases the Wave 0 native proof verified end to end.
CODEX_RUNTIME_VERIFIED_VERSIONS = frozenset({"0.154.0"})

#: ``codex --version`` prints ``codex-cli <version>`` on the verified release.
CODEX_RUNTIME_VERSION_PREFIX = "codex-cli"

#: Deadline for the read-only version probe.
CODEX_RUNTIME_PROBE_TIMEOUT_SECONDS = 10.0

_VERSION_TOKEN = re.compile(r"codex-cli[ \t]+(\S+)")

#: Backend config overrides per approval mode. Approval has no default
#: anywhere; ``None`` keeps the legacy ``plan_launch`` fallback mapping.
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
    # default policy on every codex release, so it stays the fallback here,
    # matching the legacy launch plan.
    "manual": (
        ("approval_policy", "on-request"),
        ("sandbox_mode", "read-only"),
    ),
}

#: Fallback approval overrides when the requested mode is unknown or absent.
_CODEX_APPROVAL_FALLBACK = _CODEX_APPROVAL_OVERRIDES["manual"]


def codex_endpoint_url(endpoint: str) -> str:
    """The app-server listen/remote URL for one private local endpoint.

    The daemon owns the private socket path; this helper only renders the
    wire form the native CLI expects, accepting either a bare path or an
    already-schemed endpoint string.
    """
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
    """Read-only probe of installed codex-cli compatibility.

    Verifies the unmodified binary against the frozen Wave 0 compatibility
    policy: the exact releases the native proof exercised end to end. Unknown
    or unsupported versions must select legacy wiring under ``auto``; an
    explicit ``native`` request fails with the recorded reason.
    """
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
                "wiring=auto selects legacy, explicit native fails with this reason"
            ),
        )
    if completed.returncode != 0:
        return RuntimeCompatibility(
            supported=False,
            policy=CODEX_RUNTIME_COMPATIBILITY_POLICY,
            native_version=None,
            reason=(
                f"codex --version exited with {completed.returncode}; wiring=auto selects "
                "legacy, explicit native fails with this reason"
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
                "wiring=auto selects legacy, explicit native fails with this reason"
            ),
        )
    if version not in CODEX_RUNTIME_VERIFIED_VERSIONS:
        return RuntimeCompatibility(
            supported=False,
            policy=CODEX_RUNTIME_COMPATIBILITY_POLICY,
            native_version=version,
            reason=(
                f"codex-cli {version} is not Theater-verified by compatibility policy "
                f"{CODEX_RUNTIME_COMPATIBILITY_POLICY} (verified: "
                f"{', '.join(sorted(CODEX_RUNTIME_VERIFIED_VERSIONS))}); wiring=auto selects "
                "legacy, explicit native fails with this reason"
            ),
        )
    return RuntimeCompatibility(
        supported=True,
        policy=CODEX_RUNTIME_COMPATIBILITY_POLICY,
        native_version=version,
    )


def codex_backend_config_overrides(
    context: RuntimePlanningContext,
) -> tuple[tuple[str, str], ...]:
    """Backend-scoped ``-c`` config overrides for one participant's backend.

    Approval, model, and reasoning effort are applied to the backend that runs
    the agent — the app-server process every thread in this private backend
    inherits them from — never to a frontend UI alone. No credentials appear
    in overrides.
    """
    approval = context.approval
    overrides = (
        _CODEX_APPROVAL_FALLBACK
        if approval is None
        else _CODEX_APPROVAL_OVERRIDES.get(approval, _CODEX_APPROVAL_FALLBACK)
    )
    pairs = list(overrides)
    if context.model:
        pairs.append(("model", context.model))
    if context.reasoning_effort:
        pairs.append(("model_reasoning_effort", context.reasoning_effort))
    return tuple(pairs)


def plan_codex_runtime_backend(context: RuntimePlanningContext) -> RuntimePlan:
    """Pure plan for one participant's detached Codex app-server backend.

    The backend is ``codex app-server --listen unix://<private-socket>``:
    WebSocket frames with an HTTP Upgrade handshake, not Theater NDJSON. The
    participant's approval/model/reasoning configuration rides on the backend
    via config overrides; participant-scoped MCP configuration is composed by
    the daemon through the existing MCP renderer, which inserts its own
    ``-c mcp_servers.*`` overrides ahead of the subcommand. The working
    directory is backend-scoped by the daemon launching this process in the
    participant's worktree; every thread the backend loads (including the
    UI-created one) captures that cwd.
    """
    argv: list[str] = [CODEX_BINARY]
    for key, value in codex_backend_config_overrides(context):
        argv += ["-c", f"{key}={value}"]
    argv += ["app-server", "--listen", codex_endpoint_url(context.endpoint)]
    return RuntimePlan(backend=LaunchPlan(argv=argv), endpoint=context.endpoint)


def plan_codex_frontend(endpoint: str, *, native_session_id: str | None) -> LaunchPlan:
    """Plan native CLI UI attachment only; the plan never carries a prompt.

    ``native_session_id=None`` is the promptless fresh-native-UI plan used
    before any session exists (UI-first NEW order): the stock TUI takes its
    ``StartFresh`` path and eagerly issues ``thread/start`` on this private
    backend. A non-None id attaches to that exact existing session via
    ``resume`` (FORK/RECONNECT order). Neither form submits a prompt.
    """
    argv = [CODEX_BINARY, "--remote", codex_endpoint_url(endpoint)]
    if native_session_id is not None:
        argv += ["resume", native_session_id]
    return LaunchPlan(argv=argv)
