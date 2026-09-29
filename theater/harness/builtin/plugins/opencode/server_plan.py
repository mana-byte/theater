"""Planning and qualification for the detached OpenCode server topology."""

from __future__ import annotations

import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from theater import paths
from theater.harness.contracts.launch import LaunchPlan
from theater.harness.contracts.runtime import (
    RuntimeCompatibility,
    RuntimeEndpointDiscovery,
    RuntimePlan,
    RuntimePlanningContext,
    RuntimeProbeContext,
)

from .constants import _APPROVAL_SESSION_RULES, MODELS_TIMEOUT
from .dialect import (
    OpenCodeDialect,
    domain_for,
    installed_dialect,
    resolve_binary,
    v2_database_path,
    v2_lineage_marker,
)
from .mcp import plugin_path
from .native_plugin import render_native_plugin
from .native_plugin_v2 import plugin_dir, render_native_plugin_v2
from .observer import database_path
from .runtime_plan import (
    OPENCODE_SERVER_COMPATIBILITY_POLICY,
    OPENCODE_SERVER_MAX_VERSION,
    OPENCODE_SERVER_MIN_VERSION,
    OPENCODE_SERVER_V2_COMPATIBILITY_POLICY,
    OPENCODE_SERVER_V2_MAX_VERSION,
    OPENCODE_SERVER_V2_MIN_VERSION,
    SERVER_STDOUT_MAX_BYTES,
    parse_opencode_version,
)
from .server_discovery import parse_server_stdout_endpoint

#: Evidence-pinned route facts for 1.18.29-1.18.33 (stock-binary probe; the
#: Theater-consumed upstream surfaces are byte-stable across those releases):
#: banner on stdout, Basic auth, /global/health, POST /session, GET /session/:id,
#: POST /session/:id/fork, GET /session/:id/message, /session/:id/prompt_async
#: (204 no body), and /event data-only SSE.
SERVER_LOOPBACK_HOSTNAME = "127.0.0.1"
SERVER_SECRET_ENV = "OPENCODE_SERVER_PASSWORD"


def plan_opencode_server(context: RuntimePlanningContext) -> RuntimePlan:
    """Plan the stock `opencode serve` backend with its exact launch policy.

    The runtime credential is core-minted and never in argv or env bytes.
    """
    binary = resolve_binary(context.binary or "opencode")
    if installed_dialect(binary) is OpenCodeDialect.V2:
        return _plan_opencode_server_v2(context, binary=binary)
    if context.token_file is None:
        raise ValueError(
            "the OpenCode server plan requires the core-minted runtime "
            "credential; a declared runtime credential must be minted before "
            "the backend launches"
        )
    participant_id = context.participant_id
    config_path = paths.mcp_config_path(participant_id)
    native_plugin_path = plugin_path(config_path)
    token_path = paths.participant_observation_dir(participant_id, "opencode") / "receipt-token"
    config: dict[str, object] = {
        "$schema": "https://opencode.ai/config.json",
        "plugin": [native_plugin_path.resolve().as_uri()],
    }
    if context.model:
        config["model"] = context.model
    files = {
        config_path: json.dumps(config, indent=2),
        native_plugin_path: render_native_plugin(
            participant_id,
            token_path,
            _APPROVAL_SESSION_RULES.get(context.approval or "", ()),
        ),
    }
    backend = LaunchPlan(
        argv=[
            binary,
            "serve",
            "--hostname",
            SERVER_LOOPBACK_HOSTNAME,
            "--port",
            "0",
        ],
        env={
            "OPENCODE_CONFIG": str(config_path),
            "OPENCODE_DB": str(database_path()),
        },
        files=files,
        receipt_token_path=token_path,
        secret_env={SERVER_SECRET_ENV: Path(context.token_file)},
    )
    return RuntimePlan(
        backend=backend,
        endpoint=None,
        endpoint_discovery=RuntimeEndpointDiscovery(
            parser=parse_server_stdout_endpoint,
            max_bytes=SERVER_STDOUT_MAX_BYTES,
        ),
    )


def _plan_opencode_server_v2(context: RuntimePlanningContext, *, binary: str) -> RuntimePlan:
    """The same stock `serve`, on the participant's own 2.x lineage database."""
    if context.token_file is None:
        raise ValueError(
            "the OpenCode server plan requires the core-minted runtime "
            "credential; a declared runtime credential must be minted before "
            "the backend launches"
        )
    participant_id = context.participant_id
    config_path = paths.mcp_config_path(participant_id)
    token_path = paths.participant_observation_dir(participant_id, "opencode") / "receipt-token"
    config: dict[str, object] = {
        "$schema": "https://opencode.ai/config.json",
        "plugin": [plugin_dir(config_path).resolve().as_uri()],
    }
    if context.model:
        config["model"] = context.model
    database = v2_database_path(participant_id)
    marker, marker_text = v2_lineage_marker(participant_id)
    files = {
        config_path: json.dumps(config, indent=2),
        marker: marker_text,
        **render_native_plugin_v2(participant_id, config_path, token_path, context.approval or ""),
    }
    backend = LaunchPlan(
        argv=[
            binary,
            "serve",
            "--hostname",
            SERVER_LOOPBACK_HOSTNAME,
            "--port",
            "0",
        ],
        env={"OPENCODE_CONFIG": str(config_path), "OPENCODE_DB": str(database)},
        files=files,
        receipt_token_path=token_path,
        secret_env={SERVER_SECRET_ENV: Path(context.token_file)},
        transcript_domain=domain_for(database),
    )
    return RuntimePlan(
        backend=backend,
        endpoint=None,
        endpoint_discovery=RuntimeEndpointDiscovery(
            parser=parse_server_stdout_endpoint,
            max_bytes=SERVER_STDOUT_MAX_BYTES,
        ),
    )


def probe_opencode_server_compatibility(context: RuntimeProbeContext) -> RuntimeCompatibility:
    """Qualify exactly the stock release the server topology was probed on."""
    binary = context.binary or "opencode"
    try:
        # Both read-only checks stay mandatory; the scoped pool joins them on failure too.
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="opencode-probe") as pool:
            version_future = pool.submit(_run_probe, [binary, "--version"])
            help_future = pool.submit(_run_probe, [binary, "serve", "--help"])
            version_run, help_run = version_future.result(), help_future.result()
    except (OSError, subprocess.SubprocessError) as exc:
        return _unsupported(f"could not run read-only OpenCode server probes: {exc}")
    version = parse_opencode_version(f"{version_run.stdout}\n{version_run.stderr}")
    if version_run.returncode != 0 or version is None:
        return _unsupported("opencode --version did not report a usable release")
    rendered = ".".join(str(part) for part in version)
    if version[0] == 2:
        if context.resume:
            # A resume continues in the predecessor's lineage database; the native
            # backend cannot open it, so do not start one for this spawn.
            return RuntimeCompatibility(
                supported=False,
                policy=OPENCODE_SERVER_V2_COMPATIBILITY_POLICY,
                native_version=rendered,
                reason=(
                    "an OpenCode 2.x resume continues on the legacy route, which owns the "
                    "lineage database; the native server topology has no resume"
                ),
            )
        return _qualify_v2(version, rendered, help_run)
    if not OPENCODE_SERVER_MIN_VERSION <= version < OPENCODE_SERVER_MAX_VERSION:
        return RuntimeCompatibility(
            supported=False,
            policy=OPENCODE_SERVER_COMPATIBILITY_POLICY,
            native_version=rendered,
            reason=(
                "OpenCode release is outside the server-topology compatibility "
                "range; qualified releases are 1.18.29-1.18.33"
            ),
        )
    serve_help = f"{help_run.stdout}\n{help_run.stderr}"
    if help_run.returncode != 0 or not {"--port", "--hostname"}.issubset(serve_help.split()):
        return RuntimeCompatibility(
            supported=False,
            policy=OPENCODE_SERVER_COMPATIBILITY_POLICY,
            native_version=rendered,
            reason="opencode serve did not expose the documented port-0 flags",
        )
    return RuntimeCompatibility(
        supported=True,
        policy=OPENCODE_SERVER_COMPATIBILITY_POLICY,
        native_version=rendered,
    )


def _qualify_v2(
    version: tuple[int, int, int], rendered: str, help_run: subprocess.CompletedProcess[str]
) -> RuntimeCompatibility:
    if not OPENCODE_SERVER_V2_MIN_VERSION <= version < OPENCODE_SERVER_V2_MAX_VERSION:
        return RuntimeCompatibility(
            supported=False,
            policy=OPENCODE_SERVER_V2_COMPATIBILITY_POLICY,
            native_version=rendered,
            reason=(
                "OpenCode 2.x release is outside the server-topology compatibility range; "
                "stock-binary evidence exists for 2.0.x from 2.0.18"
            ),
        )
    serve_help = f"{help_run.stdout}\n{help_run.stderr}"
    if help_run.returncode != 0 or not {"--port", "--hostname"}.issubset(serve_help.split()):
        return RuntimeCompatibility(
            supported=False,
            policy=OPENCODE_SERVER_V2_COMPATIBILITY_POLICY,
            native_version=rendered,
            reason="opencode serve did not expose the documented port-0 flags",
        )
    return RuntimeCompatibility(
        supported=True,
        policy=OPENCODE_SERVER_V2_COMPATIBILITY_POLICY,
        native_version=rendered,
    )


def _run_probe(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=MODELS_TIMEOUT,
        check=False,
    )


def _unsupported(reason: str) -> RuntimeCompatibility:
    return RuntimeCompatibility(
        supported=False,
        policy=OPENCODE_SERVER_COMPATIBILITY_POLICY,
        reason=reason,
    )


__all__ = [
    "OPENCODE_SERVER_COMPATIBILITY_POLICY",
    "plan_opencode_server",
    "probe_opencode_server_compatibility",
]
