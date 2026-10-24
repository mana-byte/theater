"""OpenCode: stable releases and installation of ``@opencode/cli`` from npm."""

from __future__ import annotations

from pathlib import Path

from .. import github
from ..contract import Sandbox
from . import npm

_PACKAGE = "@opencode/cli"


class OpenCodeAdapter:
    name = "opencode"

    def discover(self) -> list[str]:
        return github.npm_versions(_PACKAGE)

    def install(self, sb: Sandbox, version: str) -> Path:
        prefix = sb.root / "npm"
        npm.install(sb, prefix, [f"{_PACKAGE}@{version}"])
        return npm.expose(sb, "opencode", prefix / "node_modules" / ".bin" / "opencode")


ADAPTER = OpenCodeAdapter()
