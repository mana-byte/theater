"""Vibe: stable releases from PyPI installed as a uv tool, provisioned against the mock model."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from ..contract import InstallFailed, Sandbox
from ..mock.provisioning import CompatManifest, McpCall, NativeEdit, ProvisionContext, Provisioned
from ..qualification.process import run_group
from . import _registries

#: Used when the run names no logical models; two are needed to prove a selection takes effect.
_DEFAULT_MODELS = ("mock-a", "mock-b")
_PROVIDER = "mockprov"


def provision(context: ProvisionContext) -> Provisioned:
    """A config with one OpenAI-style provider serving each logical model, and a trusted workdir."""
    models = context.models or _DEFAULT_MODELS
    vibe_home = context.home / ".vibe"
    config = (
        f'active_model = "{models[0]}"\n'
        f'[[providers]]\nname = "{_PROVIDER}"\n'
        f'api_base = "{context.mock_urls["openai-chat"]}"\n'
        'api_key_env_var = ""\napi_style = "openai"\nbackend = "generic"\n'
    ) + "".join(
        f'[[models]]\nname = "{name}"\nprovider = "{_PROVIDER}"\nalias = "{name}"\n'
        for name in models
    )
    # Vibe resolves the workdir before comparing, so trust the resolved path.
    trusted = f'trusted = ["{context.workdir.resolve()}"]\nuntrusted = []\n'
    return Provisioned(
        protocol="openai-chat",
        env={"VIBE_HOME": str(vibe_home)},
        files={
            vibe_home / "config.toml": config,
            vibe_home / "trusted_folders.toml": trusted,
        },
        models=models,
    )


COMPAT = CompatManifest(
    provision=provision,
    native_edit=NativeEdit("write_file", {"path": "{path}", "content": "{nonce}\n"}),
    # Vibe offers MCP tools only through its script runner, as `tools.mcp_<server>.<tool>`.
    mcp_call=McpCall(
        "run_typescript", {"code": "return await tools.mcp_{server}.{tool}({arguments});"}
    ),
    canaries=("launch-env-dropped",),
)


#: Pinned so qualification does not depend on the runner's default interpreter.
_PYTHON = "3.14"


class VibeAdapter:
    name = "vibe"
    compat = COMPAT

    def discover(self) -> list[str]:
        return _registries.pypi_versions("mistral-vibe")

    def install(self, sb: Sandbox, version: str) -> Path:
        uv = shutil.which("uv", path=sb.env.get("PATH"))
        if uv is None:
            raise InstallFailed("uv is unavailable in the sandbox")
        env = {
            **sb.env,
            "UV_TOOL_DIR": str(sb.root / "uv-tools"),
            "UV_TOOL_BIN_DIR": str(sb.bin_dir),
            "UV_CACHE_DIR": str(sb.root / "uv-cache"),
        }
        # Pinned: a runner without Python makes uv fetch the newest one, which has no wheels.
        command = [
            uv,
            "tool",
            "install",
            "--no-config",
            "--python",
            _PYTHON,
            f"mistral-vibe=={version}",
        ]
        try:
            run = run_group(command, cwd=sb.root, env=env, timeout=240)
        except (OSError, subprocess.SubprocessError) as exc:
            raise InstallFailed(
                f"uv tool install mistral-vibe=={version} did not run: {exc}"
            ) from exc
        binary = sb.bin_dir / "vibe"
        if run.returncode != 0 or not binary.is_file():
            raise InstallFailed(
                f"uv tool install mistral-vibe=={version} exited {run.returncode}: "
                f"{run.stderr[-1000:]}"
            )
        return binary


ADAPTER = VibeAdapter()
