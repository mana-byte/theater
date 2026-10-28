"""Private launch material for Pi's stock-extension frontend bridge.

The daemon mints tokens; this renders a mode-0600 config so the secret stays out of argv.
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlparse

from theater import paths
from theater.harness.contracts.runtime import RuntimeFrontendInstallContext, RuntimeFrontendOverlay

PI_FRONTEND_PROTOCOL = "theater-frontend-v1"
PI_FRONTEND_CONFIG_FILENAME = "frontend-bridge.json"
PI_FRONTEND_MAX_VALUE_CHARS = 512


def frontend_config_path(participant_id: str) -> Path:
    """Return the participant-owned private bridge descriptor location."""
    return paths.participant_observation_dir(participant_id, "pi") / PI_FRONTEND_CONFIG_FILENAME


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
    "frontend_config_path",
    "install_pi_frontend",
]
