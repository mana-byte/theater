"""Claude Code release qualification against its isolated interactive CLI."""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import MappingProxyType
from typing import ClassVar

from .. import framework, github
from ..contract import (
    Check,
    InstallFailed,
    MockReply,
    MockToolCall,
    Result,
    Sandbox,
    VerifiedSet,
    verdict_from_checks,
)
from ..route_facts import RouteFacts
from . import claude_bump

_PACKAGE = "@anthropic-ai/claude-code"
_HINT = re.compile(r"transcript|hook|session|--session-id|turn_duration", re.IGNORECASE)
_GREEN_TESTS = (
    "tests/compat/test_claude_run.py::test_claude_seeded_tui_turn",
    "tests/compat/test_claude_run.py::test_claude_observer_seals_tui_turn",
    "tests/compat/test_claude_run.py::test_claude_tool_hook_decodes_live_payload",
)


def seed_config(config_dir: Path, cwd: Path, version: str) -> None:
    """Seed Claude's onboarding and trust state inside the sandbox only."""
    config_dir.mkdir(parents=True, exist_ok=True)
    state = {
        "hasCompletedOnboarding": True,
        "lastOnboardingVersion": version,
        "projects": {str(cwd.resolve()): {"hasTrustDialogAccepted": True}},
    }
    (config_dir / ".claude.json").write_text(json.dumps(state) + "\n", encoding="utf-8")
    (config_dir / "settings.json").write_text('{"theme":"dark"}\n', encoding="utf-8")


def changelog_hints(version: str, changelog: str) -> tuple[str, ...]:
    """Return relevant release notes, bounded to the requested version's section."""
    heading = re.compile(r"^##\s+\[?([0-9]+\.[0-9]+\.[0-9]+)\]?(?:\s|$)")
    active = False
    hints: list[str] = []
    for line in changelog.splitlines():
        match = heading.match(line)
        if match:
            if active:
                break
            active = match.group(1) == version
        elif active and _HINT.search(line):
            hints.append(line.strip())
    return tuple(hints)


class ClaudeAdapter:
    name = "claude"
    probe = "theater.harness.builtin.plugins.claude.compatibility:probe_claude_hooks"
    stub_help = ""
    green_tests = _GREEN_TESTS
    consistency_tests = (
        "tests/harness/claude/test_hook_compatibility.py::test_claude_hook_version_range[hook-floor]",
        "tests/harness/claude/test_hook_compatibility.py::test_claude_hook_version_range[hook-max]",
        "tests/harness/claude/test_hook_compatibility.py::test_claude_hook_version_range[hook-above-max]",
    )
    expected_skips: ClassVar[Mapping[str, str]] = MappingProxyType({})
    expected_routes = frozenset(
        {("POST", "/v1/messages"), ("POST", "/v1/messages/count_tokens"), ("GET", "/v1/models")}
    )
    ignorable_routes: frozenset[tuple[str, str]] = frozenset()
    shadow_suite = False

    def discover(self) -> list[str]:
        return github.npm_versions(_PACKAGE)

    def verified(self, root: Path) -> VerifiedSet:
        return claude_bump.verified(root)

    def install(self, sb: Sandbox, version: str) -> Path:
        prefix = sb.root / "npm"
        try:
            result = subprocess.run(
                [
                    "npm",
                    "install",
                    "--prefix",
                    str(prefix),
                    "--no-audit",
                    "--no-fund",
                    f"{_PACKAGE}@{version}",
                ],
                env=dict(sb.env),
                capture_output=True,
                text=True,
                timeout=300,
                check=False,
            )
            installed = prefix / "node_modules" / ".bin" / "claude"
            if result.returncode != 0 or not installed.exists():
                raise InstallFailed(
                    f"npm could not install Claude Code {version}: {result.stderr[-500:]}"
                )
            binary = sb.bin_dir / "claude"
            binary.symlink_to(installed.resolve())
            reported = subprocess.run(
                [str(binary), "--version"],
                env=dict(sb.offline_env),
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            if reported.returncode != 0 or reported.stdout.strip() != self.version_line(version):
                raise InstallFailed(f"Claude Code {version} reported {reported.stdout.strip()!r}")
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise InstallFailed(f"Claude Code {version} install failed: {exc}") from exc
        return binary

    def version_line(self, version: str) -> str:
        return f"{version} (Claude Code)"

    def apply_bump(self, root: Path, greens: Sequence[str]) -> list[Path]:
        return claude_bump.bump(root, greens)

    def qualify(self, root: Path, version: str, sb: Sandbox, binary: Path) -> Result:
        cwd = sb.root / "project"
        cwd.mkdir(parents=True, exist_ok=True)
        (cwd / "hook-proof.txt").write_text("hook-proof\n", encoding="utf-8")
        seed_config(sb.config_dir, cwd, version)
        env = {
            **sb.offline_env,
            "THEATER_HOME": str(sb.root / "theater"),
            "CLAUDE_CONFIG_DIR": str(sb.config_dir),
            "ANTHROPIC_BASE_URL": sb.mock_base_urls["anthropic"],
            "ANTHROPIC_AUTH_TOKEN": "dummy-token",
            "ANTHROPIC_MODEL": "mock-model",
            "DISABLE_AUTOUPDATER": "1",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "DISABLE_TELEMETRY": "1",
            "HARNESS_COMPAT_BINARY": str(binary),
            "HARNESS_COMPAT_WORKDIR": str(cwd),
            "HARNESS_COMPAT_TMUX_SOCKET": str(sb.tmux_socket),
            "HARNESS_COMPAT_EVIDENCE_DIR": str(sb.evidence_dir),
        }
        # Claude asks for confirmation when ANTHROPIC_API_KEY is set, even if it is a dummy.
        env.pop("ANTHROPIC_API_KEY", None)
        consistency = framework.run_pytest(root, self.consistency_tests, env=env, expected_skips={})
        if any(check.status != "pass" for check in consistency):
            return Result(self.name, version, "tooling", consistency, evidence_dir=sb.evidence_dir)
        try:
            notes = changelog_hints(
                version, github.raw_file("anthropics/claude-code", "main", "CHANGELOG.md")
            )
        except (OSError, ValueError):
            notes = ()
        green_checks = list(
            framework.run_pytest(
                root, self.green_tests[:2], env=env, expected_skips=self.expected_skips
            )
        )
        sb.mock.queue(
            "anthropic",
            MockReply(
                text="",
                tool_calls=(MockToolCall("Read", {"file_path": str(cwd / "hook-proof.txt")}),),
            ),
        )
        green_checks.extend(
            framework.run_pytest(
                root, self.green_tests[2:], env=env, expected_skips=self.expected_skips
            )
        )
        if notes:
            green_checks[0] = Check(
                green_checks[0].name,
                green_checks[0].status,
                green_checks[0].detail + "\n" + "\n".join(notes),
            )
        checks = [*consistency, *green_checks]
        verdict = verdict_from_checks(green_checks)
        if any(
            check.status == "skip" and check.name not in self.expected_skips
            for check in green_checks
        ):
            verdict = "inconclusive"
        unexpected = RouteFacts(sb.mock.requests, self.expected_routes, self.ignorable_routes).gaps
        if verdict != "green" and unexpected:
            routes = sorted({RouteFacts.route(request) for request in unexpected})
            checks.append(Check("mock_routes", "error", f"unexpected/unhandled routes: {routes}"))
            verdict = "tooling"
        return Result(self.name, version, verdict, tuple(checks), evidence_dir=sb.evidence_dir)


ADAPTER = ClaudeAdapter()
