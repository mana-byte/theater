"""Vibe: stable releases from PyPI and installation as a uv tool."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from .. import github
from ..contract import InstallFailed, Sandbox
from ..process import run_group

#: Pinned so qualification does not depend on the runner's default interpreter.
_PYTHON = "3.14"


class VibeAdapter:
    name = "vibe"

    def discover(self) -> list[str]:
        return github.pypi_versions("mistral-vibe")

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
