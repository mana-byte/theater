"""OpenCode 2.x adapter: the same detached-server topology, proven model-free on the real binary.

2.x ships as ``@opencode/cli`` (the 1.x line stays on ``opencode-ai``) and its ceiling is a
separate literal, so it is a separate harness for the pipeline: its own cache, PR and issue.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from types import MappingProxyType

from .. import framework, github
from ..contract import (
    Adapter,
    Check,
    Result,
    Route,
    Sandbox,
    VerifiedSet,
    verdict_from_checks,
    version_key,
)
from . import opencode2_bump, opencode_sandbox

NPM_PACKAGE = "@opencode/cli"

_PROOF = "tests/integration/opencode/test_server_v2_proof.py"
_PROOF_TESTS = (
    "test_v2_banner_basic_auth_and_info",
    "test_v2_session_location_and_idle_absence",
    "test_v2_prompt_admission_is_durable_and_projects_into_the_lineage_database",
    "test_v2_generated_approval_plugin_is_active_on_the_selected_server",
)


class OpenCodeV2Adapter:
    """The OpenCode 2.x arm of the frozen adapter contract."""

    name = "opencode2"
    #: The plugin directory the evidence digest covers; it is shared with the 1.x arm.
    plugin = "opencode"
    # The probe the manifest wires; its 2.x branch needs only the serve port flags.
    probe = (
        "theater.harness.builtin.plugins.opencode.server_plan:probe_opencode_server_compatibility"
    )
    stub_help = "--port --hostname"
    green_tests: tuple[str, ...] = tuple(f"{_PROOF}::{test}" for test in _PROOF_TESTS)
    consistency_tests: tuple[str, ...] = (
        "tests/compat/test_opencode2.py",
        "tests/harness/opencode/server/test_version_probe.py::test_probe_qualifies_exactly_the_2x_window",
    )
    #: Model-free: the proof never asks the mock for a completion.
    expected_routes: frozenset[Route] = frozenset()
    ignorable_routes: frozenset[Route] = frozenset()
    expected_skips: Mapping[str, str] = MappingProxyType({})

    def discover(self) -> list[str]:
        """Every stable release of ``@opencode/cli`` on the 2.x line, ascending."""
        return [
            version for version in github.npm_versions(NPM_PACKAGE) if version_key(version)[0] == 2
        ]

    def verified(self, root: Path) -> VerifiedSet:
        """The 2.x range gate parsed out of the tree; never imports theater."""
        return opencode2_bump.verified_set(root)

    def install(self, sb: Sandbox, version: str) -> Path:
        """npm-install exactly ``version`` and expose its bin under ``sb.bin_dir``."""
        return opencode_sandbox.npm_install(sb, NPM_PACKAGE, version)

    def version_line(self, version: str) -> str:
        """2.x prints ``opencode v<release>``."""
        return f"opencode v{version}"

    def apply_bump(self, root: Path, greens: Sequence[str]) -> list[Path]:
        """Admit the greens by raising the plugin's exclusive 2.x ceiling."""
        return opencode2_bump.apply_bump(root, list(greens))

    def qualify(self, root: Path, version: str, sb: Sandbox, binary: Path) -> Result:
        """Run the model-free server proofs; consistency failures are tooling failures."""
        del binary
        env = {**sb.offline_env, "THEATER_OPENCODE_V2_SERVER_CONFORMANCE": "1"}
        consistency = framework.run_pytest(root, self.consistency_tests, env=env, expected_skips={})
        if any(check.status != "pass" for check in consistency):
            return Result(self.name, version, "tooling", consistency, sb.evidence_dir)
        proving = framework.run_pytest(
            root, self.green_tests, env=env, expected_skips=self.expected_skips
        )
        verdict = verdict_from_checks(proving)
        checks = (*consistency, *proving)
        stray = {(request.method, request.path) for request in sb.mock.requests}
        if verdict != "green" and stray:
            checks += (Check("mock routes", "error", f"unexpected={sorted(stray)!r}"),)
            verdict = "tooling"
        return Result(self.name, version, verdict, checks, sb.evidence_dir)


ADAPTER: Adapter = OpenCodeV2Adapter()
