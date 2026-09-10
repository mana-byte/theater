"""Isolation scaffolding for the Codex native UI bootstrap proof.

Builds the private world one smoke run needs: an isolated `CODEX_HOME` whose
`config.toml` mirrors the mock provider the upstream codex-rs test suite uses
(`app-server/tests/common/config.rs`, `MockResponsesConfig::write`), plus a
throwaway git repo for the backend's working directory. Nothing here touches
the developer's real `~/.codex`, the Theater daemon, or any tmux server that is
not named by the test itself.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

MOCK_PROVIDER_ID = "mock_provider"
MOCK_MODEL = "mock-model"

_CONFIG_TEMPLATE = """\
model = "{model}"
model_provider = "{provider_id}"
approval_policy = "on-request"
sandbox_mode = "read-only"
project_trust_level = "trusted"

[model_providers.{provider_id}]
name = "Mock provider for Theater native proof"
base_url = "{base_url}"
wire_api = "responses"
request_max_retries = 0
stream_max_retries = 0
"""


def write_mock_config(codex_home: Path, base_url: str) -> Path:
    """Write the mock-provider config.toml the app-server and TUI will load."""
    codex_home.mkdir(parents=True, exist_ok=True)
    config_path = codex_home / "config.toml"
    config_path.write_text(
        _CONFIG_TEMPLATE.format(model=MOCK_MODEL, provider_id=MOCK_PROVIDER_ID, base_url=base_url),
        encoding="utf-8",
    )
    return config_path


def make_git_repo(path: Path) -> Path:
    """A one-commit repo: a cwd with real git metadata, safe to work in."""
    path.mkdir(parents=True, exist_ok=True)
    run = subprocess.run
    run(["git", "init", "-q"], cwd=path, check=True)
    run(
        ["git", "config", "user.email", "proof@theater.local"],
        cwd=path,
        check=True,
    )
    run(["git", "config", "user.name", "Theater Proof"], cwd=path, check=True)
    (path / "README.md").write_text("theater codex native ui bootstrap proof\n", encoding="utf-8")
    run(["git", "add", "README.md"], cwd=path, check=True)
    run(["git", "commit", "-q", "-m", "proof repo"], cwd=path, check=True)
    return path


def codex_binary() -> str | None:
    return shutil.which("codex")


def codex_version() -> str | None:
    binary = codex_binary()
    if binary is None:
        return None
    result = subprocess.run(
        [binary, "--version"], capture_output=True, text=True, timeout=30, check=False
    )
    return result.stdout.strip() or None
