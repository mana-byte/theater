"""Range qualification for the Vibe CLI and its unified store."""

from __future__ import annotations

import re
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

from .. import framework, github
from ..contract import (
    Check,
    InstallFailed,
    Result,
    Sandbox,
    Verdict,
    VerifiedSet,
    load_evidenced,
)
from .vibe_bump import bump, gate
from .vibe_store import reader_minor, store_check

_PROOF = "tests/test_harness_compat_vibe.py"
_GREEN_TESTS = (
    f"{_PROOF}::test_real_vibe_store_format",
    f"{_PROOF}::test_real_vibe_mock_turn",
    f"{_PROOF}::test_real_vibe_reader_turn",
)


class VibeAdapter:
    name = "vibe"
    kind = "range"
    probe = "theater.harness.builtin.plugins.vibe.compatibility:probe_vibe_compatibility"
    stub_help = ""
    green_tests = _GREEN_TESTS
    consistency_tests = ("tests/test_vibe_compatibility.py",)
    expected_skips: Mapping[str, str] = {}
    expected_routes = frozenset({("POST", "/v1/chat/completions")})
    ignorable_routes: frozenset[tuple[str, str]] = frozenset()

    def discover(self) -> list[str]:
        return github.pypi_versions("mistral-vibe")

    def verified(self, root: Path) -> VerifiedSet:
        floor, ceiling = gate(root)
        return VerifiedSet(
            kind="range",
            floor=floor,
            ceiling=ceiling,
            evidenced=load_evidenced(root, self.name),
        )

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
        try:
            result = subprocess.run(
                [uv, "tool", "install", "--no-config", f"mistral-vibe=={version}"],
                env=env,
                capture_output=True,
                text=True,
                timeout=240,
                check=False,
            )
            binary = sb.bin_dir / "vibe"
            probe = (
                subprocess.run(
                    [binary, "--version"],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=20,
                    check=False,
                )
                if result.returncode == 0 and binary.is_file()
                else None
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise InstallFailed(str(exc)) from exc
        if (
            probe is None
            or probe.returncode != 0
            or probe.stdout.strip() != self.version_line(version)
        ):
            detail = (
                result.stderr[-1000:] if result.returncode else "installed binary version mismatch"
            )
            raise InstallFailed(detail)
        return binary

    def version_line(self, version: str) -> str:
        return f"vibe {version}"

    def apply_bump(self, root: Path, greens: Sequence[str]) -> list[Path]:
        return bump(root, greens)

    def pre_bump_check(self, root: Path, version: str) -> Check:
        return store_check(root, version)

    def qualify(self, root: Path, version: str, sb: Sandbox, binary: Path) -> Result:
        static = self.pre_bump_check(root, version)
        if static.status != "pass":
            verdict: Verdict = "red" if static.status == "fail" else "inconclusive"
            return Result(self.name, version, verdict, (static,), sb.evidence_dir)
        vibe_home = sb.config_dir / "vibe"
        vibe_home.mkdir(parents=True, exist_ok=True)
        config = (
            'active_model = "mock"\n'
            '[[providers]]\nname = "mockprov"\n'
            f'api_base = "{sb.mock_base_urls["openai"]}"\n'
            'api_key_env_var = ""\napi_style = "openai"\nbackend = "generic"\n'
            '[[models]]\nname = "mock-model"\nprovider = "mockprov"\nalias = "mock"\n'
        )
        (vibe_home / "config.toml").write_text(config)
        upstream_match = re.search(r"upstream (\d+)", static.detail)
        assert upstream_match is not None
        env = {
            **sb.offline_env,
            "VIBE_HOME": str(vibe_home),
            "VIBE_COMPAT_BINARY": str(binary),
            "VIBE_COMPAT_HOME": str(vibe_home),
            "VIBE_COMPAT_TMUX_SOCKET": str(sb.tmux_socket),
            "VIBE_COMPAT_REQUESTS": str(sb.mock_requests),
            "VIBE_COMPAT_UPSTREAM_MINOR": upstream_match.group(1),
            "VIBE_COMPAT_READER_MINOR": str(reader_minor(root)),
        }
        checks = (
            static,
            *framework.run_pytest(
                root, self.green_tests, env=env, expected_skips=self.expected_skips
            ),
        )
        proof_checks = checks[1:]
        if tuple(check.name for check in proof_checks) != self.green_tests:
            verdict = "tooling"
            checks += (Check("pytest_nodes", "error", "runner did not return the requested nodes"),)
        elif any(check.status == "fail" for check in proof_checks):
            verdict = "red"
        elif any(check.status in {"skip", "error"} for check in proof_checks):
            verdict = "inconclusive"
        else:
            verdict = "green"
        routes = {(request.method, request.path) for request in sb.mock.requests}
        unexpected = routes - self.expected_routes - self.ignorable_routes
        if verdict != "green" and (sb.mock.unhandled or unexpected):
            checks += (Check("mock_route", "error", f"unexpected routes: {sorted(unexpected)}"),)
            verdict = "tooling"
        return Result(self.name, version, verdict, checks, sb.evidence_dir)


ADAPTER = VibeAdapter()
