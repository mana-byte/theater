"""Codex native runtime plan: version probe gate, approval refusals and the yolo mapping."""

from __future__ import annotations

import pytest

from tests.support.codex_server import (
    CWD,
    ENDPOINT,
    GENERATION,
    PARTICIPANT,
    ScriptedCodexIO,
    ScriptedCodexServer,
)
from theater.harness.builtin.plugins.codex.manifest import MANIFEST
from theater.harness.builtin.plugins.codex.runtime import CodexRuntime
from theater.harness.builtin.plugins.codex.runtime_plan import (
    CODEX_RUNTIME_COMPATIBILITY_POLICY,
    VERSION_POLICY,
    codex_backend_config_overrides,
    plan_codex_frontend,
    plan_codex_runtime_backend,
    probe_codex_compatibility,
    qualified_versions_range_text,
)
from theater.harness.contracts.runtime import (
    RuntimeContext,
    RuntimePlanningContext,
    RuntimeProbeContext,
)


class _FakeCompleted:
    def __init__(self, output: str, returncode: int = 0) -> None:
        self.stdout = output
        self.stderr = ""
        self.returncode = returncode


def _patch_version(
    monkeypatch, output: str | None, *, error: Exception | None = None, returncode: int = 0
) -> None:
    def fake_run(argv, **kwargs):
        assert argv[1:2] == ["--version"]
        if error is not None:
            raise error
        return _FakeCompleted(output or "", returncode)

    monkeypatch.setattr(
        "theater.harness.builtin.plugins.codex.runtime_plan.subprocess.run", fake_run
    )


@pytest.mark.parametrize(
    ("version", "supported"),
    [
        (VERSION_POLICY.minimum, True),
        ("0.0.1", False),
        # A matched-yet-unstable banner must fail closed, not fall through as supported.
        (f"{VERSION_POLICY.minimum}-alpha.1", False),
    ],
)
def test_probe_range_gate_and_manifest_range(monkeypatch, version: str, supported: bool) -> None:
    _patch_version(monkeypatch, f"codex-cli {version}\n")
    compatibility = probe_codex_compatibility(RuntimeProbeContext(binary="codex"))
    assert compatibility.supported is supported
    assert compatibility.policy == CODEX_RUNTIME_COMPATIBILITY_POLICY
    assert compatibility.native_version == version
    if supported:
        assert compatibility.reason is None
    else:
        # The same refusal prevents a provider launch from bypassing admission.
        assert compatibility.reason is not None
        assert version in compatibility.reason
        assert f"qualified range {qualified_versions_range_text()}" in compatibility.reason
    qualified_range = MANIFEST.native_compatibility.qualified_range
    assert qualified_range == qualified_versions_range_text()
    assert qualified_range == VERSION_POLICY.range_text


@pytest.mark.parametrize(
    ("output", "error", "returncode", "reason_part"),
    [
        ("some other tool 1.2.3\n", None, 0, "did not name a codex-cli version"),
        (None, FileNotFoundError("codex"), 0, "could not run"),
        ("boom", None, 1, "exited with 1"),
    ],
    ids=["unparseable-output", "missing-binary", "failed-version-command"],
)
def test_probe_rejects_a_binary_without_a_readable_version(
    monkeypatch, output, error, returncode, reason_part
) -> None:
    _patch_version(monkeypatch, output, error=error, returncode=returncode)
    compatibility = probe_codex_compatibility(RuntimeProbeContext(binary="/no/such/codex"))
    assert compatibility.supported is False
    assert compatibility.native_version is None
    assert reason_part in compatibility.reason


def planning_context(**overrides) -> RuntimePlanningContext:
    values: dict[str, object] = {
        "participant_id": PARTICIPANT,
        "cwd": CWD,
        "endpoint": ENDPOINT,
        "config_path": None,
        "approval": "manual",
        "model": None,
        "reasoning_effort": None,
    }
    values.update(overrides)
    return RuntimePlanningContext(**values)  # type: ignore[arg-type]


# Yolo's full-access sandbox mapping has no live oracle; pin it here.
def test_yolo_maps_to_a_full_access_sandbox() -> None:
    assert codex_backend_config_overrides(planning_context(approval="yolo")) == (
        ("approval_policy", "never"),
        ("sandbox_mode", "danger-full-access"),
    )


# Approval is explicit per spawn and has no default anywhere: a missing or unknown mode is
# rejected, never silently mapped to manual.
@pytest.mark.parametrize(
    "plan",
    [
        lambda: codex_backend_config_overrides(planning_context(approval=None)),
        lambda: codex_backend_config_overrides(planning_context(approval="nonsense")),
        lambda: plan_codex_runtime_backend(planning_context(approval=None)),
        lambda: plan_codex_frontend(ENDPOINT, native_session_id=None, approval=None),
    ],
    ids=["overrides-missing", "overrides-unknown", "backend-missing", "frontend-missing"],
)
def test_backend_plan_rejects_missing_or_unknown_approval(plan) -> None:
    with pytest.raises(ValueError, match="approval has no default"):
        plan()


def test_backend_plan_carries_no_credentials_or_files() -> None:
    plan = plan_codex_runtime_backend(planning_context())
    assert plan.backend.env == {}
    assert plan.backend.files == {}
    assert plan.backend.private_files == {}


async def test_backend_and_frontend_use_the_same_selected_binary(tmp_path):
    binary = str(tmp_path / "codex-qualified")
    backend = plan_codex_runtime_backend(planning_context(binary=binary))
    assert backend.backend.argv[0] == binary
    runtime = CodexRuntime(
        RuntimeContext(
            participant_id=PARTICIPANT,
            cwd=CWD,
            io=ScriptedCodexIO(ScriptedCodexServer()),
            backend_generation=GENERATION,
            endpoint=ENDPOINT,
            approval="manual",
            binary=binary,
        )
    )
    try:
        frontend = await runtime.frontend_plan(native_session_id=None)
        assert frontend.argv[0] == binary
    finally:
        await runtime.aclose()
