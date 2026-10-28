"""Codex native runtime plan: version probe, backend and frontend launch plans, manifest."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.support.codex_server import (
    CWD,
    ENDPOINT,
    GENERATION,
    PARTICIPANT,
    ScriptedCodexIO,
    ScriptedCodexServer,
    thread_started,
)
from theater.harness.builtin.plugins.codex.manifest import MANIFEST, manifest_for_root
from theater.harness.builtin.plugins.codex.runtime import CodexRuntime, codex_runtime_factory
from theater.harness.builtin.plugins.codex.runtime_plan import (
    CODEX_RUNTIME_COMPATIBILITY_POLICY,
    VERSION_POLICY,
    codex_backend_config_overrides,
    codex_endpoint_url,
    plan_codex_frontend,
    plan_codex_runtime_backend,
    probe_codex_compatibility,
    qualified_versions_range_text,
)
from theater.harness.contracts.channels import ChannelKind
from theater.harness.contracts.manifest import HarnessManifest
from theater.harness.contracts.runtime import (
    LiveChannelDeclaration,
    RuntimeCompatibility,
    RuntimeContext,
    RuntimeManifest,
    RuntimePlan,
    RuntimePlanningContext,
    RuntimeProbeContext,
    SessionOpenMode,
)
from theater.harness.contracts.version_policy import version_key
from theater.harness.manifests.validation import validate_manifest


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


def _text(release: tuple[int, int, int]) -> str:
    return ".".join(map(str, release))


def _gate_cases() -> list[tuple[str, bool]]:
    maximum = version_key(VERSION_POLICY.maximum)
    return [
        (VERSION_POLICY.minimum, True),
        (VERSION_POLICY.maximum, True),
        ("0.0.1", False),
        (_text((*maximum[:2], maximum[2] + 1)), False),
        *((version, False) for version in VERSION_POLICY.excluded),
        (f"{VERSION_POLICY.minimum}-alpha.1", False),
        (f"0{VERSION_POLICY.minimum}", False),
    ]


@pytest.mark.parametrize(("version", "supported"), _gate_cases())
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
        assert "install a qualified release" in compatibility.reason
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


def test_backend_plan_is_exact_app_server_listen_with_scoped_config() -> None:
    plan = plan_codex_runtime_backend(
        planning_context(approval="yolo", model="gpt-5.2", reasoning_effort="high")
    )
    assert isinstance(plan, RuntimePlan)
    assert plan.backend.argv == [
        "codex",
        "-c",
        "approval_policy=never",
        "-c",
        "sandbox_mode=danger-full-access",
        "-c",
        "model=gpt-5.2",
        "-c",
        "model_reasoning_effort=high",
        "app-server",
        "--listen",
        f"unix://{ENDPOINT}",
    ]
    assert plan.endpoint == ENDPOINT


@pytest.mark.parametrize(
    ("approval", "expected"),
    [
        ("edits", (("approval_policy", "on-request"), ("sandbox_mode", "workspace-write"))),
        ("manual", (("approval_policy", "on-request"), ("sandbox_mode", "read-only"))),
        ("yolo", (("approval_policy", "never"), ("sandbox_mode", "danger-full-access"))),
    ],
    ids=["edits", "manual", "yolo"],
)
def test_backend_plan_approval_modes_match_legacy_mapping(approval, expected) -> None:
    assert codex_backend_config_overrides(planning_context(approval=approval)) == expected


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


def test_endpoint_url_helper() -> None:
    assert codex_endpoint_url("/tmp/x.sock") == "unix:///tmp/x.sock"
    assert codex_endpoint_url("unix:///tmp/x.sock") == "unix:///tmp/x.sock"


def test_frontend_plan_promptless_for_fresh_ui() -> None:
    plan = plan_codex_frontend(
        ENDPOINT,
        native_session_id=None,
        approval="yolo",
        model="gpt-5.2",
        reasoning_effort="high",
    )
    assert plan.argv == [
        "codex",
        "-c",
        "check_for_update_on_startup=false",
        "-c",
        "approval_policy=never",
        "-c",
        "sandbox_mode=danger-full-access",
        "-c",
        "model=gpt-5.2",
        "-c",
        "model_reasoning_effort=high",
        "--remote",
        f"unix://{ENDPOINT}",
    ]
    assert "resume" not in plan.argv


def test_frontend_plan_resumes_exact_thread() -> None:
    plan = plan_codex_frontend(ENDPOINT, native_session_id="th-42", approval="manual")
    assert plan.argv == [
        "codex",
        "-c",
        "check_for_update_on_startup=false",
        "-c",
        "approval_policy=on-request",
        "-c",
        "sandbox_mode=read-only",
        "--remote",
        f"unix://{ENDPOINT}",
        "resume",
        "th-42",
    ]


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


def test_manifest_declares_valid_runtime_manifest() -> None:
    runtime = MANIFEST.runtime
    assert isinstance(runtime, RuntimeManifest)
    assert callable(runtime.probe)
    assert callable(runtime.plan)
    assert callable(runtime.factory)
    assert isinstance(runtime.channel, LiveChannelDeclaration)
    assert runtime.channel.channel.kind is ChannelKind.LIVE
    assert runtime.channel.drives_job_completion is True
    assert runtime.channel.durable_fallback is True
    observation_ids = {channel.id for channel in MANIFEST.observation.channels}
    assert runtime.channel.channel.id not in observation_ids
    validate_manifest("codex", MANIFEST)


def test_manifest_for_root_preserves_runtime() -> None:
    manifest = manifest_for_root(Path("/tmp/root"))
    assert isinstance(manifest, HarnessManifest)
    assert manifest.runtime is MANIFEST.runtime


async def test_manifest_factory_creates_runtime_from_context() -> None:
    server = ScriptedCodexServer()
    context = RuntimeContext(
        participant_id=PARTICIPANT,
        cwd=CWD,
        io=ScriptedCodexIO(server),
        backend_generation=GENERATION,
        endpoint=ENDPOINT,
        approval="manual",
    )
    runtime = codex_runtime_factory(context)
    assert isinstance(runtime, CodexRuntime)
    server.push(thread_started())
    binding = await runtime.open_session(mode=SessionOpenMode.NEW)
    assert binding.participant_id == PARTICIPANT
    await runtime.aclose()


async def test_planning_matches_manifest_runtime_plan() -> None:
    plan = MANIFEST.runtime.plan(planning_context(approval="edits"))
    assert isinstance(plan, RuntimePlan)
    assert plan.backend.argv[-1] == f"unix://{ENDPOINT}"
    compatibility = MANIFEST.runtime.probe(RuntimeProbeContext(binary="/nonexistent"))
    assert isinstance(compatibility, RuntimeCompatibility)
    assert compatibility.supported is False
