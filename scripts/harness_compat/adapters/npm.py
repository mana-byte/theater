"""Shared npm installation of an upstream CLI into a sandbox."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from .. import github
from ..contract import InstallFailed, Sandbox
from ..process import run_group

_TIMEOUT_SECONDS = 600


@dataclass(frozen=True, slots=True)
class NpmAdapter:
    name: str
    package: str

    def discover(self) -> list[str]:
        return github.npm_versions(self.package)

    def install(self, sb: Sandbox, version: str) -> Path:
        prefix = sb.root / "npm"
        install(sb, prefix, [f"{self.package}@{version}"])
        return expose(sb, self.name, prefix / "node_modules" / ".bin" / self.name)


def install(
    sb: Sandbox,
    prefix: Path,
    specs: Sequence[str],
) -> None:
    """``npm install`` ``specs`` under ``prefix`` with the sandbox's network-capable environment."""
    command = ["npm", "install", "--prefix", str(prefix), "--no-audit", "--no-fund", *specs]
    label = " ".join(specs)
    try:
        run = run_group(command, cwd=sb.root, env=sb.env, timeout=_TIMEOUT_SECONDS)
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        raise InstallFailed(f"npm install {label} did not run: {exc}") from exc
    if run.returncode != 0:
        detail = (run.stderr or run.stdout).strip()[-500:]
        raise InstallFailed(f"npm install {label} exited {run.returncode}: {detail}")


def expose(sb: Sandbox, name: str, target: Path) -> Path:
    """Link ``target`` as ``name`` on the sandbox's PATH and return the link."""
    if not target.exists():
        raise InstallFailed(f"npm install produced no {target.name}")
    link = sb.bin_dir / name
    link.unlink(missing_ok=True)
    try:
        link.symlink_to(target.resolve())
    except OSError as exc:
        raise InstallFailed(f"could not expose {name}: {exc}") from exc
    return link
