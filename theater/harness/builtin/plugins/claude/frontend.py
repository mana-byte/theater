"""Launch-local Claude plugin and private native connection descriptor."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlparse

from theater import paths
from theater.constants.harness import HARNESS_RUNTIME_RESULT_MAX_CHARS
from theater.harness.contracts.runtime import RuntimeFrontendInstallContext, RuntimeFrontendOverlay

from .runtime_protocol import PROTOCOL


def install_claude_frontend(context: RuntimeFrontendInstallContext) -> RuntimeFrontendOverlay:
    endpoint = urlparse(context.endpoint)
    if (
        endpoint.scheme != "unix"
        or endpoint.netloc
        or not endpoint.path.startswith("/")
        or endpoint.query
        or endpoint.fragment
    ):
        raise ValueError("Claude frontend requires its private absolute Unix socket")
    root = paths.participant_observation_dir(context.participant_id, "claude")
    plugin = root / "native-plugin"
    descriptor = root / "native-frontend.json"
    bundled = Path(__file__).with_name("native_plugin")
    files = {
        plugin / name: (bundled / name).read_text(encoding="utf-8")
        for name in (".claude-plugin/plugin.json", "hooks/hooks.json", "hooks/register.ts")
    }
    files[descriptor] = (
        json.dumps(
            {
                "protocol": PROTOCOL,
                "participant_id": context.participant_id,
                "socket": endpoint.path,
                "token_file": str(context.token_file),
                "max_result_chars": HARNESS_RUNTIME_RESULT_MAX_CHARS,
            },
            sort_keys=True,
        )
        + "\n"
    )
    return RuntimeFrontendOverlay(
        args=("--plugin-dir", str(plugin)),
        env={"THEATER_CLAUDE_FRONTEND_CONFIG": str(descriptor)},
        files=files,
    )
