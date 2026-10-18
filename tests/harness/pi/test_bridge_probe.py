"""Pi frontend bridge overlay, version probe and the packaged extension."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass

import pytest

from tests.support.paths import REPO_ROOT
from tests.support.pi_bridge import (
    PARTICIPANT,
)
from theater.harness.builtin.plugins.pi import runtime as pi_runtime_module
from theater.harness.builtin.plugins.pi.frontend import (
    PI_FRONTEND_PROTOCOL,
    PiFrontendBridgeConfig,
    with_frontend_bridge,
)
from theater.harness.builtin.plugins.pi.launch import plan_launch
from theater.harness.builtin.plugins.pi.runtime_constants import (
    PI_FRONTEND_QUALIFIED_EXCLUDED,
    PI_FRONTEND_QUALIFIED_FLOOR,
    PI_FRONTEND_QUALIFIED_MAX,
    PI_FRONTEND_QUALIFIED_RANGE,
    PI_FRONTEND_RUNTIME_COMPATIBILITY_POLICY,
    pi_version_text,
)
from theater.harness.contracts.callbacks import LaunchContext
from theater.harness.contracts.runtime import (
    RuntimeProbeContext,
)


def test_pi_frontend_bridge_overlay_keeps_token_out_of_argv_and_public_files(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("THEATER_HOME", str(tmp_path / "theater-home"))
    plan = plan_launch(LaunchContext(PARTICIPANT, "inspect", tmp_path / "mcp.json", "yolo"))
    token = "token-do-not-put-in-argv-0123456789"

    bridged = with_frontend_bridge(
        plan,
        PiFrontendBridgeConfig(
            participant_id=PARTICIPANT,
            endpoint="tcp://127.0.0.1:43123",
            token=token,
        ),
    )

    assert "--theater-frontend-config" not in plan.argv
    assert "--theater-frontend-config" in bridged.argv
    assert all(token not in value for value in bridged.argv)
    assert all(token not in value for value in bridged.files.values())
    assert len(bridged.private_files) == 1
    private = json.loads(next(iter(bridged.private_files.values())))
    assert private == {
        "endpoint": "tcp://127.0.0.1:43123",
        "participant_id": PARTICIPANT,
        "protocol": PI_FRONTEND_PROTOCOL,
        "token": token,
    }


@dataclass
class _Completed:
    stdout: str
    stderr: str = ""
    returncode: int = 0


def _admitted(*releases: tuple[int, int, int]) -> list[str]:
    """The releases the qualified range admits, as version texts, without repeats."""
    kept: list[tuple[int, int, int]] = []
    for release in releases:
        inside = PI_FRONTEND_QUALIFIED_FLOOR <= release <= PI_FRONTEND_QUALIFIED_MAX
        if inside and release not in PI_FRONTEND_QUALIFIED_EXCLUDED and release not in kept:
            kept.append(release)
    return [pi_version_text(release) for release in kept]


_FLOOR_MAJOR, _FLOOR_MINOR, _FLOOR_PATCH = PI_FRONTEND_QUALIFIED_FLOOR
_QUALIFIED_PI_VERSIONS = (
    *_admitted(
        PI_FRONTEND_QUALIFIED_FLOOR,
        (_FLOOR_MAJOR, _FLOOR_MINOR, _FLOOR_PATCH + 1),
        # A two-digit patch still orders numerically, not as text.
        (_FLOOR_MAJOR, _FLOOR_MINOR, 10),
        (_FLOOR_MAJOR, _FLOOR_MINOR + 1, 0),
        PI_FRONTEND_QUALIFIED_MAX,
    ),
    # Immediate neighbours of every excluded release stay accepted.
    *(
        pi_version_text((major, minor, patch + offset))
        for major, minor, patch in PI_FRONTEND_QUALIFIED_EXCLUDED
        for offset in (-1, 1)
        if patch + offset >= 0
        and (major, minor, patch + offset) not in PI_FRONTEND_QUALIFIED_EXCLUDED
        and PI_FRONTEND_QUALIFIED_FLOOR
        <= (major, minor, patch + offset)
        <= PI_FRONTEND_QUALIFIED_MAX
    ),
)


@pytest.mark.parametrize("version", _QUALIFIED_PI_VERSIONS)
def test_pi_frontend_probe_accepts_qualified_versions(version, monkeypatch) -> None:
    calls: list[tuple[str, ...]] = []

    def run(argv, **kwargs):
        del kwargs
        calls.append(tuple(argv))
        assert argv[1] == "--version"
        return _Completed(f"{version}\n")

    monkeypatch.setattr(subprocess, "run", run)

    compatibility = pi_runtime_module.probe_pi_frontend_compatibility(
        RuntimeProbeContext(binary="pi")
    )

    assert compatibility.supported is True
    assert compatibility.policy == PI_FRONTEND_RUNTIME_COMPATIBILITY_POLICY
    assert compatibility.native_version == version
    assert calls == [("pi", "--version")]


@pytest.mark.parametrize(
    "version",
    [
        pi_version_text(
            (
                PI_FRONTEND_QUALIFIED_FLOOR[0],
                PI_FRONTEND_QUALIFIED_FLOOR[1],
                PI_FRONTEND_QUALIFIED_FLOOR[2] - 1,
            )
        ),
        # Every excluded broken release.
        *(pi_version_text(entry) for entry in PI_FRONTEND_QUALIFIED_EXCLUDED),
        pi_version_text(
            (
                PI_FRONTEND_QUALIFIED_MAX[0],
                PI_FRONTEND_QUALIFIED_MAX[1],
                PI_FRONTEND_QUALIFIED_MAX[2] + 1,
            )
        ),
        pi_version_text((PI_FRONTEND_QUALIFIED_MAX[0], PI_FRONTEND_QUALIFIED_MAX[1] + 1, 0)),
        pi_version_text((PI_FRONTEND_QUALIFIED_MAX[0] + 1, 0, 0)),
    ],
)
def test_pi_frontend_probe_refuses_unqualified_versions(version, monkeypatch) -> None:
    monkeypatch.setattr(subprocess, "run", lambda argv, **kwargs: _Completed(f"{version}\n"))
    refused = pi_runtime_module.probe_pi_frontend_compatibility(RuntimeProbeContext(binary="pi"))
    assert refused.supported is False
    assert refused.policy == PI_FRONTEND_RUNTIME_COMPATIBILITY_POLICY
    assert refused.reason is not None
    if int(version.split(".", 1)[0]) <= PI_FRONTEND_QUALIFIED_MAX[0]:
        assert refused.native_version == version
        assert PI_FRONTEND_QUALIFIED_RANGE in refused.reason
    else:
        assert refused.native_version is None


def test_pi_extension_uses_public_lifecycle_and_settings_surfaces_only() -> None:
    bridge = (REPO_ROOT / "theater/harness/builtin/plugins/pi/theater_mcp_bridge.ts").read_text(
        encoding="utf-8"
    )

    assert "AgentSession" not in bridge
    assert 'pi.on("agent_settled"' in bridge
    assert 'pi.on("agent_end"' in bridge
    assert "model_update_proof_gated" in bridge
    # The awaiting-input display contract: a user-input tool call sets the
    # footer, the matching tool_result and every turn boundary clear it.
    assert 'pi.on("tool_call"' in bridge
    assert 'pi.on("tool_result"' in bridge
    assert "theater.pi.awaiting" in bridge
    assert "ask_user_question" in bridge
    assert "await this.pi.setModel(" not in bridge
    assert "this.pi.getThinkingLevel()" in bridge
    assert "this.pi.setThinkingLevel(" in bridge
    assert "--theater-frontend-config" in bridge
    assert "FRONTEND_OWNER" in bridge
    assert "bridge_epoch" in bridge
    assert 'pi.on("session_before_compact"' in bridge
    assert 'bridge.transition(ctx, "agent_end", "active")' in bridge


@pytest.mark.requires_node
def test_pi_extension_frontend_bridge_executable_conformance() -> None:
    """Drive the actual extension registration through fresh public contexts."""
    root = REPO_ROOT
    result = subprocess.run(
        [
            "node",
            "--experimental-strip-types",
            "tests/fixtures/pi_frontend_bridge_conformance.mts",
        ],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert "pi frontend bridge executable conformance: ok" in result.stdout
