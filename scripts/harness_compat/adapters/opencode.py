"""OpenCode 1.x adapter; the server proof uses the sandbox model mock.

Detached runtime lacks a TUI installer, so that gate requires human review.
"""

from __future__ import annotations

import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import MappingProxyType

from .. import framework, github
from ..contract import (
    Adapter,
    Check,
    InstallFailed,
    Result,
    Route,
    Sandbox,
    Verdict,
    VerifiedSet,
    verdict_from_checks,
    version_key,
)
from . import opencode_bump, opencode_pi_process, opencode_sandbox

NPM_PACKAGE = "opencode-ai"

_SERVER_PROOF = "tests/integration/test_opencode_server_proof.py"
_STOCK_PROOF = "tests/integration/test_opencode_stock_ui.py"

_SERVER_PROOF_TESTS = (
    "test_port0_banner_and_basic_auth_reach_every_surface",
    "test_exact_session_identity_and_idle_absence",
    "test_prompt_admission_lineage_busy_gate_and_release",
    "test_sse_reconnect_and_history_readback_never_duplicate",
    "test_fork_while_parent_is_active",
    "test_attach_authentication_and_server_survival",
    "test_server_mode_plugin_bus_parity",
)
_STOCK_PROOF_TESTS = (
    "test_stock_tui_loads_passive_extension_and_reconnects",
    "test_stock_tui_accepts_native_send_with_exact_lineage",
)
_TUI_SKIP_DETAIL = (
    "stock-TUI proof NOT run: detached runtime lacks a frontend installer "
    "(theater/daemon/spawning/planning.py:325-327); TUI gate bump rests on server proofs, "
    "probe and --help flags only; human review required"
)
_REGIE_SKIP = Check(
    "packages/regie/tests/test_dashboard_rich.py",
    "skip",
    "regie dashboard formatting is tested separately; isolated pytest cannot import regie",
)


class OpenCodeAdapter:
    """The OpenCode 1.x arm of the frozen adapter contract."""

    name = "opencode"
    probe = "theater.harness.builtin.plugins.opencode.runtime_plan:probe_opencode_compatibility"
    stub_help = "--model --auto --fork"
    green_tests: tuple[str, ...] = (
        *(f"{_SERVER_PROOF}::{test}" for test in _SERVER_PROOF_TESTS),
        *(f"{_STOCK_PROOF}::{test}" for test in _STOCK_PROOF_TESTS),
    )
    consistency_tests: tuple[str, ...] = (
        "tests/compat/test_opencode.py",
        "tests/harness/opencode/test_server_plan.py::test_probe_qualifies_exactly_the_1x_window",
        "tests/harness/opencode/test_server_runtime.py::test_server_handshake_rejects_version_drift",
        "tests/harness/opencode/test_frontend.py::test_probe_rejects_releases_outside_the_tested_window",
    )
    #: The routed mistral provider talks to the mock's OpenAI chat surface.
    expected_routes: frozenset[Route] = frozenset({("POST", "/v1/chat/completions")})
    ignorable_routes: frozenset[Route] = frozenset()
    expected_skips: Mapping[str, str] = MappingProxyType(
        {
            f"{_STOCK_PROOF}::{test}": "set THEATER_OPENCODE_STOCK_CONFORMANCE=1"
            for test in _STOCK_PROOF_TESTS
        }
    )

    def discover(self) -> list[str]:
        """Every stable npm release of opencode-ai on the 1.x line, ascending."""
        return [
            version for version in github.npm_versions(NPM_PACKAGE) if version_key(version)[0] < 2
        ]

    def verified(self, root: Path) -> VerifiedSet:
        """The range gate parsed out of the tree; never imports theater."""
        return opencode_bump.verified_set(root)

    def install(self, sb: Sandbox, version: str) -> Path:
        """npm-install exactly ``version`` and expose its bin under ``sb.bin_dir``."""
        prefix = sb.root / "npm-opencode"
        try:
            run = opencode_pi_process.run_group(
                ["npm", "install", "--prefix", str(prefix), f"{NPM_PACKAGE}@{version}"],
                env=dict(sb.env),
                timeout=600,
            )
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            raise InstallFailed(f"npm install {NPM_PACKAGE}@{version} did not run: {exc}") from exc
        if run.returncode != 0:
            raise InstallFailed(
                f"npm install {NPM_PACKAGE}@{version} exited {run.returncode}: "
                f"{(run.stderr or run.stdout).strip()[-400:]}"
            )
        packaged = prefix / "node_modules" / ".bin" / "opencode"
        if not packaged.exists():
            raise InstallFailed(f"npm install {NPM_PACKAGE}@{version} produced no bin/opencode")
        exposed = sb.bin_dir / "opencode"
        exposed.unlink(missing_ok=True)
        exposed.symlink_to(packaged)
        try:
            check = opencode_pi_process.run_group(
                [str(exposed), "--version"], env=sb.offline_env, timeout=60
            )
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            raise InstallFailed(f"installed opencode version check failed: {exc}") from exc
        output = f"{check.stdout}{check.stderr}".strip()
        if check.returncode != 0 or version not in output:
            raise InstallFailed(f"installed opencode did not report {version!r}: {output!r}")
        return exposed

    def version_line(self, version: str) -> str:
        """1.x prints a bare release; the probe regex tolerates an optional ``v``."""
        return version

    def apply_bump(self, root: Path, greens: Sequence[str]) -> list[Path]:
        """Admit the greens by raising the plugin's exclusive ceiling."""
        return opencode_bump.apply_bump(root, list(greens))

    def qualify(self, root: Path, version: str, sb: Sandbox, binary: Path) -> Result:
        """Run offline proofs after pre-warming OpenCode's catalog.

        Pre-warm and consistency failures are tooling failures.
        """
        opencode_sandbox.write_provider_override(sb)
        fixture = root / "tests" / "fixtures" / "opencode_probe_plugin.mjs"
        if not fixture.is_file() or not opencode_sandbox.prewarm_catalog(sb, plugin=fixture):
            return Result(
                harness=self.name,
                version=version,
                verdict="tooling",
                checks=(Check("opencode prewarm", "error", "plugin fixture or cache unavailable"),),
                evidence_dir=sb.evidence_dir,
            )
        env = {
            **sb.offline_env,
            "THEATER_OPENCODE_SERVER_CONFORMANCE": "1",
            "THEATER_OPENCODE_STOCK_CONFORMANCE": "",
        }
        consistency = framework.run_pytest(
            root,
            self.consistency_tests,
            env=env,
            expected_skips={},
        )
        if any(check.status != "pass" for check in consistency):
            return Result(self.name, version, "tooling", consistency, sb.evidence_dir)
        proving = framework.run_pytest(
            root,
            self.green_tests,
            env=env,
            expected_skips=self.expected_skips,
        )
        proving = tuple(
            Check(check.name, check.status, f"{check.detail}; {_TUI_SKIP_DETAIL}".strip("; "))
            if check.name in self.expected_skips and check.status == "skip"
            else check
            for check in proving
        )
        checks = consistency + proving + (_REGIE_SKIP,)
        verdict: Verdict = verdict_from_checks(proving)
        if verdict == "green" and any(
            check.status == "skip" for check in proving if check.name.startswith(_SERVER_PROOF)
        ):
            verdict = "inconclusive"
        if verdict != "green":
            stray = {
                (request.method, request.path)
                for request in sb.mock.requests
                if (request.method, request.path)
                not in self.expected_routes | self.ignorable_routes
            }
            if stray:
                verdict = "tooling"
                checks += (Check("mock routes", "error", f"unexpected={sorted(stray)!r}"),)
        return Result(
            harness=self.name,
            version=version,
            verdict=verdict,
            checks=tuple(checks),
            evidence_dir=sb.evidence_dir,
        )


ADAPTER: Adapter = OpenCodeAdapter()
