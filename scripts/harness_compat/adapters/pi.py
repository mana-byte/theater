"""Compatibility qualification for the published Pi coding agent."""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import framework, github
from ..contract import Check, InstallFailed, Result, Sandbox, VerifiedSet, verdict_from_checks
from ..process import run_pipe_group
from ..route_facts import RouteFacts
from . import pi_bump, pi_pins

_PACKAGE = "@earendil-works/pi-coding-agent"


@dataclass(frozen=True, slots=True)
class PiAdapter:
    name: str = "pi"
    probe: str = "theater.harness.builtin.plugins.pi.runtime_probe:probe_pi_frontend_compatibility"
    stub_help: str = ""
    green_tests: tuple[str, ...] = (
        "tests/integration/pi/test_native_control.py::test_pi_core_correlation_proof_holds_on_stock_pi",
        "tests/integration/pi/test_native_control.py::test_pi_active_run_interrupt_proof_holds_on_stock_pi",
        "tests/integration/pi/test_native_control.py::test_pi_steer_admission_proof_holds_on_stock_pi",
    )
    consistency_tests: tuple[str, ...] = (
        "tests/compat/test_pi.py",
        "tests/harness/pi/test_bridge_probe.py::test_pi_frontend_probe_accepts_qualified_versions",
        "tests/harness/pi/test_bridge_probe.py::test_pi_frontend_probe_refuses_unqualified_versions",
    )
    expected_skips: Mapping[str, str] = field(default_factory=dict)
    expected_routes: frozenset[tuple[str, str]] = frozenset()
    ignorable_routes: frozenset[tuple[str, str]] = frozenset()

    def discover(self) -> list[str]:
        return github.npm_versions(_PACKAGE)

    def verified(self, root: Path) -> VerifiedSet:
        return pi_bump.verified(root)

    def install(self, sb: Sandbox, version: str) -> Path:
        self._npm_install(sb, version, (f"{_PACKAGE}@{version}",))
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

        # npm resolves carets to the newest release; the proofs need upstream's own set.
        if pins := pi_pins.pinned_specs(document.get("dependencies")):
            self._npm_install(sb, version, (f"{_PACKAGE}@{version}", *pins))

        binary = sb.bin_dir / "pi"
        try:
            binary.symlink_to(target)
        except OSError as exc:
            raise InstallFailed(f"could not expose installed Pi {version} binary: {exc}") from exc
        return binary

    def _npm_install(self, sb: Sandbox, version: str, specs: Sequence[str]) -> None:
        command = (
            "npm",
            "install",
            "--prefix",
            str(sb.bin_dir),
            "--no-save",
            "--ignore-scripts",
            "--no-audit",
            "--no-fund",
            *specs,
        )
        try:
            completed = run_pipe_group(command, env=dict(sb.env), timeout=300)
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            raise InstallFailed(f"npm could not install Pi {version}: {exc}") from exc
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()[-500:]
            raise InstallFailed(f"npm could not install Pi {version}: {detail}")

    def version_line(self, version: str) -> str:
        return version

    def apply_bump(self, root: Path, greens: Sequence[str]) -> list[Path]:
        return pi_bump.apply_bump(root, greens)

    def qualify(self, root: Path, version: str, sb: Sandbox, binary: Path) -> Result:
        env = dict(sb.offline_env)
        env["PATH"] = f"{binary.parent}{os.pathsep}{env.get('PATH', '')}"
        consistency = framework.run_pytest(
            root,
            self.consistency_tests,
            env=env,
            expected_skips={},
        )
        if any(check.status != "pass" for check in consistency):
            return Result(self.name, version, "tooling", consistency, sb.evidence_dir)

        proofs = framework.run_pytest(
            root,
            self.green_tests,
            env=env,
            expected_skips=self.expected_skips,
        )
        checks = consistency + proofs
        unexpected = RouteFacts(
            sb.mock.requests, self.expected_routes, self.ignorable_routes
        ).unexpected_routes
        unhandled = RouteFacts(
            sb.mock.unhandled, self.expected_routes, self.ignorable_routes
        ).unignored_routes
        if unexpected or unhandled:
            detail = f"unexpected={sorted(unexpected)!r}; unhandled={sorted(unhandled)!r}"
            return Result(
                self.name,
                version,
                "tooling",
                (*checks, Check("mock routes", "error", detail)),
                sb.evidence_dir,
            )
        return Result(
            self.name,
            version,
            verdict_from_checks(proofs),
            checks,
            sb.evidence_dir,
        )


ADAPTER = PiAdapter()
