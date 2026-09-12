"""Private launch material for Pi's stock-extension frontend bridge.

The bridge is deliberately an additive overlay on Pi's ordinary interactive
launch.  The daemon-side frontend host owns endpoint allocation and token
minting; this module only renders its participant-local, mode-0600 config and
keeps the secret out of argv and public launch files.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from urllib.parse import urlparse

from theater import paths
from theater.harness.contracts.launch import LaunchPlan
from theater.harness.contracts.runtime import RuntimeFrontendInstallContext, RuntimeFrontendOverlay

PI_FRONTEND_PROTOCOL = "theater-frontend-v1"
PI_FRONTEND_CONFIG_FILENAME = "frontend-bridge.json"
PI_FRONTEND_MAX_VALUE_CHARS = 512


def frontend_config_path(participant_id: str) -> Path:
    """Return the participant-owned private bridge descriptor location."""
    return paths.participant_observation_dir(participant_id, "pi") / PI_FRONTEND_CONFIG_FILENAME


def _bounded(value: object, label: str, *, limit: int = PI_FRONTEND_MAX_VALUE_CHARS) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"Pi frontend {label} must be a bounded non-blank string")
    return value


def _validate_loopback_endpoint(endpoint: str) -> None:
    """Accept only a local Unix socket or the private IPv4 loopback endpoint."""
    parsed = urlparse(endpoint)
    if (
        parsed.scheme == "unix"
        and not parsed.netloc
        and parsed.path.startswith("/")
        and not parsed.params
        and not parsed.query
        and not parsed.fragment
    ):
        return
    try:
        port = parsed.port
    except ValueError:
        port = None
    if (
        parsed.scheme != "tcp"
        or parsed.hostname != "127.0.0.1"
        or port is None
        or not 1 <= port <= 65535
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "Pi frontend endpoint must be a local unix URI or tcp://127.0.0.1:<port>; "
            "the extension bridge never dials a non-loopback endpoint"
        )


@dataclass(frozen=True, slots=True)
class PiFrontendBridgeConfig:
    """The one secret-bearing descriptor consumed by the bundled extension."""

    participant_id: str
    endpoint: str
    token: str = field(repr=False)

    def __post_init__(self) -> None:
        _bounded(self.participant_id, "participant id")
        _bounded(self.endpoint, "endpoint")
        # A short bearer value is not an authentication secret.  The upper
        # bound also keeps one NDJSON hello safely below its frame limit.
        _bounded(self.token, "token")
        if len(self.token) < 16:
            raise ValueError("Pi frontend token must contain at least 16 characters")
        _validate_loopback_endpoint(self.endpoint)

    def render(self) -> str:
        """Render a stable private descriptor without leaking it into diagnostics."""
        return (
            json.dumps(
                {
                    "protocol": PI_FRONTEND_PROTOCOL,
                    "participant_id": self.participant_id,
                    "endpoint": self.endpoint,
                    "token": self.token,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        )


def with_frontend_bridge(plan: LaunchPlan, config: PiFrontendBridgeConfig) -> LaunchPlan:
    """Add the private bridge descriptor to an otherwise ordinary Pi plan.

    This function is intentionally not called by :func:`plan_launch`: until
    the daemon-side authenticated frontend host is composed, Pi keeps its
    existing legacy launch/control path unchanged.  The future host calls this
    helper after it has allocated a loopback endpoint and minted the token.
    """
    if not isinstance(plan, LaunchPlan):
        raise TypeError("Pi frontend bridge requires a LaunchPlan")
    path = frontend_config_path(config.participant_id)
    if any(arg == "--theater-frontend-config" for arg in plan.argv):
        raise ValueError("Pi launch plan already contains a frontend bridge config")
    if path in plan.files or path in plan.private_files:
        raise ValueError("Pi frontend bridge config collides with an existing launch file")
    return replace(
        plan,
        argv=[*plan.argv, "--theater-frontend-config", str(path)],
        private_files={**plan.private_files, path: config.render()},
    )


def install_pi_frontend(context: RuntimeFrontendInstallContext) -> RuntimeFrontendOverlay:
    """Point the stock bundled extension at the daemon's authenticated host.

    This descriptor contains a credential path, never the credential itself.
    The ordinary launch already loads the extension for MCP and durable markers.
    """
    _validate_loopback_endpoint(context.endpoint)
    path = frontend_config_path(context.participant_id)
    return RuntimeFrontendOverlay(
        env={"THEATER_PI_FRONTEND_CONFIG": str(path)},
        files={
            path: json.dumps(
                {
                    "protocol": PI_FRONTEND_PROTOCOL,
                    "participant_id": context.participant_id,
                    "endpoint": context.endpoint,
                    "token_file": str(context.token_file),
                },
                sort_keys=True,
            )
            + "\n"
        },
    )


__all__ = [
    "PI_FRONTEND_CONFIG_FILENAME",
    "PI_FRONTEND_MAX_VALUE_CHARS",
    "PI_FRONTEND_PROTOCOL",
    "PiFrontendBridgeConfig",
    "frontend_config_path",
    "install_pi_frontend",
    "with_frontend_bridge",
]
