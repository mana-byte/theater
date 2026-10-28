"""Pi: stable releases and the exact stock package installed from npm."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .. import github
from ..contract import InstallFailed, Sandbox
from . import npm

_PACKAGE = "@earendil-works/pi-coding-agent"


class PiAdapter:
    name = "pi"

    def discover(self) -> list[str]:
        return github.npm_versions(_PACKAGE)

    def install(self, sb: Sandbox, version: str) -> Path:
        spec = f"{_PACKAGE}@{version}"
        npm.install(sb, sb.bin_dir, [spec])
        package_root = sb.bin_dir / "node_modules" / "@earendil-works" / "pi-coding-agent"
        try:
            document: dict[str, Any] = json.loads(
                (package_root / "package.json").read_text(encoding="utf-8")
            )
            declared = document["bin"]
            relative = declared["pi"] if isinstance(declared, dict) else declared
            target = (package_root / relative).resolve(strict=True)
            target.relative_to(package_root.resolve())
        except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise InstallFailed(
                f"installed Pi {version} has no valid pi package bin: {exc}"
            ) from exc
        return npm.expose(sb, "pi", target)


ADAPTER = PiAdapter()
