"""Claude Code release qualification against its isolated interactive CLI."""

from __future__ import annotations

import ast
import json
import re
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
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
    evidence_file,
    load_evidenced,
    verdict_from_checks,
)

_PACKAGE = "@anthropic-ai/claude-code"
_PROBE_SOURCE = Path("theater/harness/builtin/plugins/claude/compatibility.py")
_HINT = re.compile(r"transcript|hook|session|--session-id|turn_duration", re.IGNORECASE)
_GREEN_TESTS = (
    "tests/test_harness_compat_claude_run.py::test_claude_seeded_tui_turn",
    "tests/test_harness_compat_claude_run.py::test_claude_observer_seals_tui_turn",
    "tests/test_harness_compat_claude_run.py::test_claude_tool_hook_decodes_live_payload",
)
_TOOL_SKIP = "mock tool-call replies require the framework stream"


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


def _gate(root: Path) -> tuple[str, str | None]:
    tree = ast.parse((root / _PROBE_SOURCE).read_text(encoding="utf-8"))
    probe = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "probe_claude_hooks"
    )
    call = next(
        node.value
        for node in ast.walk(probe)
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Call)
    )
    values = {
        keyword.arg: ast.literal_eval(keyword.value)
        for keyword in call.keywords
        if keyword.arg in {"floor", "upper"}
    }
    floor = ".".join(map(str, values["floor"]))
    ceiling = values.get("upper")
    return floor, ".".join(map(str, ceiling)) if ceiling is not None else None


class ClaudeAdapter:
    name = "claude"
    probe = "theater.harness.builtin.plugins.claude.compatibility:probe_claude_hooks"
    stub_help = ""
    green_tests = _GREEN_TESTS
    consistency_tests = ("tests/test_hook_compatibility.py::test_claude_hook_version_range",)
    expected_skips: ClassVar[Mapping[str, str]] = {_GREEN_TESTS[2]: _TOOL_SKIP}
    expected_routes = frozenset(
        {("POST", "/v1/messages"), ("POST", "/v1/messages/count_tokens"), ("GET", "/v1/models")}
    )
    ignorable_routes: frozenset[tuple[str, str]] = frozenset()

    def discover(self) -> list[str]:
        return github.npm_versions(_PACKAGE)

    def verified(self, root: Path) -> VerifiedSet:
        floor, ceiling = _gate(root)
        return VerifiedSet(
            "range", floor=floor, ceiling=ceiling, evidenced=load_evidenced(root, self.name)
        )

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
        changed: list[Path] = []
        for version in sorted(set(greens)):
            path = evidence_file(root, self.name, version)
            if path.exists():
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(Result(self.name, version, "green").to_evidence(), indent=2) + "\n",
                encoding="utf-8",
            )
            changed.append(path.relative_to(root))
        return changed

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
        try:
            notes = changelog_hints(
                version, github.raw_file("anthropics/claude-code", "main", "CHANGELOG.md")
            )
        except (OSError, ValueError):
            notes = ()
        checks = list(
            framework.run_pytest(
                root, self.green_tests[:2], env=env, expected_skips=self.expected_skips
            )
        )
        try:
            sb.mock.queue(
                "anthropic",
                MockReply(
                    text="",
                    tool_calls=(MockToolCall("Read", {"file_path": str(cwd / "hook-proof.txt")}),),
                ),
            )
        except NotImplementedError:
            checks.append(Check(self.green_tests[2], "skip", _TOOL_SKIP))
        else:
            checks.extend(
                framework.run_pytest(
                    root,
                    self.green_tests[2:],
                    env={**env, "HARNESS_COMPAT_TOOL_REPLY": "1"},
                    expected_skips=self.expected_skips,
                )
            )
        green_checks = list(checks)
        consistency = framework.run_pytest(root, self.consistency_tests, env=env, expected_skips={})
        checks.extend(consistency)
        if notes:
            checks[0] = Check(
                checks[0].name, checks[0].status, checks[0].detail + "\n" + "\n".join(notes)
            )
        verdict = verdict_from_checks(green_checks)
        if any(
            check.status == "skip" and check.name not in self.expected_skips
            for check in green_checks
        ):
            verdict = "inconclusive"
        if any(check.status != "pass" for check in consistency):
            verdict = "tooling"
        unexpected = [
            request
            for request in sb.mock.requests
            if (request.method, request.path) not in self.expected_routes | self.ignorable_routes
            or (not request.handled and (request.method, request.path) not in self.ignorable_routes)
        ]
        if verdict != "green" and unexpected:
            routes = sorted({(request.method, request.path) for request in unexpected})
            checks.append(Check("mock_routes", "error", f"unexpected/unhandled routes: {routes}"))
            verdict = "tooling"
        return Result(self.name, version, verdict, tuple(checks), evidence_dir=sb.evidence_dir)


ADAPTER = ClaudeAdapter()
