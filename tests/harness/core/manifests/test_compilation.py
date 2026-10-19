"""Compiling a manifest forwards typed callbacks, launch capabilities and overlay MCP rendering."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from tests.support.manifest_rig import StubSource, manifest, plan, primary_channel, screen, source
from theater.harness.contracts.callbacks import (
    LaunchContext,
    ModelDiscoveryContext,
    ResumeContext,
    ResumePreflightContext,
    ScreenContext,
)
from theater.harness.contracts.context import ParticipantObservationContext
from theater.harness.contracts.harness import Harness, LaunchParameterSupport
from theater.harness.contracts.launch import (
    LaunchPlan,
    McpRenderContext,
    McpRenderOverlay,
    ResumeLaunchOverlay,
)
from theater.harness.contracts.manifest import (
    LaunchManifest,
    McpForegroundWait,
    McpRenderingManifest,
    ModelDiscoveryManifest,
    ObservationManifest,
    ScreenManifest,
    SourceManifest,
)
from theater.harness.contracts.observation import (
    HarnessObserver,
    ScreenConfidence,
    ScreenKind,
    ScreenReading,
)
from theater.harness.contracts.source import Source
from theater.harness.manifests.compiler import compile_manifest
from theater.harness.registry.capabilities import (
    supports_model,
    supports_reasoning,
    supports_resume,
)
from theater.models import BadRequest, Participant
from theater.provenance import TranscriptProvenance


def test_compilation_forwards_typed_callbacks_and_runtime_contracts(tmp_path: Path) -> None:
    seen_launch: list[LaunchContext] = []
    seen_source: list[ParticipantObservationContext] = []
    seen_screen: list[ScreenContext] = []
    seen_models: list[ModelDiscoveryContext] = []
    seen_resume: list[ResumeContext] = []
    seen_preflight: list[ResumePreflightContext] = []
    opened = StubSource()

    def launch_callback(context: LaunchContext) -> LaunchPlan:
        seen_launch.append(context)
        return LaunchPlan(argv=["acme", context.prompt])

    def source_callback(context: ParticipantObservationContext) -> Source:
        seen_source.append(context)
        return opened

    def screen_callback(context: ScreenContext) -> ScreenReading:
        seen_screen.append(context)
        return ScreenReading(ScreenKind.PROMPT, ScreenConfidence.HIGH)

    def model_callback(context: ModelDiscoveryContext) -> tuple[str, ...]:
        seen_models.append(context)
        return ("alpha", "beta")

    def resume_callback(context: ResumeContext) -> ResumeLaunchOverlay:
        seen_resume.append(context)
        return ResumeLaunchOverlay(env={"RESUMED": "1"})

    def preflight_callback(context: ResumePreflightContext) -> None:
        seen_preflight.append(context)

    built = manifest(
        launch=LaunchManifest(
            planner=launch_callback,
            approvals=("manual", "edits"),
            supports_model=True,
            supports_reasoning_effort=True,
            supports_resume=True,
            resume_preflight=preflight_callback,
            resume_planner=resume_callback,
            resume_takes_prompt=False,
            resume_strategy="fork",
        ),
        observation=ObservationManifest(
            primary=SourceManifest(factory=source_callback, channel=primary_channel()),
            screen=ScreenManifest(classifier=screen_callback),
        ),
        models=ModelDiscoveryManifest(discoverer=model_callback),
    )
    harness = compile_manifest("acme", built)
    context = ParticipantObservationContext(
        participant_id="participant",
        cwd="/work",
        session_id="session",
        after=2.5,
        session_provenance=TranscriptProvenance.EXACT,
        known_location="/work/record",
        transcript_domain="domain",
        pane_pid=42,
    )
    predecessor = Participant(id="predecessor", transcript_domain=None)
    owner = Participant(id="owner")

    assert isinstance(harness, Harness)
    assert isinstance(harness.observer, HarnessObserver)
    assert supports_model(harness)
    assert supports_reasoning(harness)
    assert supports_resume(harness)
    assert harness.resume_takes_prompt is False
    assert harness.resume_strategy == "fork"
    assert harness.plan_launch(
        participant_id="participant",
        prompt="hello",
        config_path=tmp_path / "config.json",
        approval="edits",
        model="model-a",
        reasoning_effort="high",
        resume="native-session",
    ).argv == ["acme", "hello"]
    assert harness.observer.open_source_context(context) is opened
    assert harness.observer.screen_reading("ready> ") == ScreenReading(
        ScreenKind.PROMPT,
        ScreenConfidence.HIGH,
    )
    assert harness.discover_models() == ["alpha", "beta"]
    assert harness.resume_preflight(predecessor=predecessor) is None
    assert harness.resume_launch_overlay(
        predecessor=predecessor,
        trusted_session_owners=[predecessor, owner],
    ) == ResumeLaunchOverlay(env={"RESUMED": "1"})

    assert seen_launch == [
        LaunchContext(
            participant_id="participant",
            prompt="hello",
            config_path=tmp_path / "config.json",
            approval="edits",
            model="model-a",
            reasoning_effort="high",
            resume="native-session",
            binary="acme",
        )
    ]
    assert seen_source == [context]

    harness.observer.open_source(cwd="/work")
    assert seen_source[-1].participant_scoped is False
    assert seen_screen == [ScreenContext(capture="ready> ")]
    assert seen_models == [ModelDiscoveryContext(name="acme", binary="acme")]
    assert seen_preflight == [ResumePreflightContext(predecessor=predecessor)]
    assert seen_resume == [
        ResumeContext(predecessor=predecessor, trusted_session_owners=(predecessor, owner))
    ]


@pytest.mark.parametrize(
    ("model", "reasoning", "resume"),
    [(True, False, True), (True, True, True)],
    ids=["opencode-vibe", "claude-codex"],
)
def test_compiled_launch_capabilities_match_the_declaration(
    model: bool,
    reasoning: bool,
    resume: bool,
) -> None:
    seen: list[LaunchContext] = []

    def callback(context: LaunchContext) -> LaunchPlan:
        seen.append(context)
        return LaunchPlan(argv=["acme"])

    harness = compile_manifest(
        "acme",
        manifest(
            launch=LaunchManifest(
                planner=callback,
                approvals=("manual",),
                supports_model=model,
                supports_reasoning_effort=reasoning,
                supports_resume=resume,
            )
        ),
    )

    assert supports_model(harness) is model
    assert supports_reasoning(harness) is reasoning
    assert supports_resume(harness) is resume
    assert harness.launch_parameter_support == LaunchParameterSupport(
        model=model,
        reasoning_effort=reasoning,
        resume=resume,
        approvals=("manual",),
    )
    options = {"model": "model-a", "resume": "native-session"}
    if reasoning:
        options["reasoning_effort"] = "high"
    harness.plan_launch(
        participant_id="participant",
        prompt="hello",
        config_path=Path("config.json"),
        approval="manual",
        **options,
    )
    assert seen == [
        LaunchContext(
            participant_id="participant",
            prompt="hello",
            config_path=Path("config.json"),
            approval="manual",
            model="model-a" if model else None,
            reasoning_effort="high" if reasoning else None,
            resume="native-session" if resume else None,
            binary="acme",
        )
    ]


@pytest.mark.parametrize(
    ("option", "value", "message"),
    [
        ("model", "model-a", "model selection"),
        ("reasoning_effort", "high", "reasoning effort selection"),
        ("resume", "native-session", "resume"),
    ],
)
def test_compiled_harness_rejects_direct_unsupported_launch_options(
    option: str,
    value: str,
    message: str,
) -> None:
    harness = compile_manifest("acme", manifest())

    with pytest.raises(BadRequest, match=f"does not support {message}"):
        harness.plan_launch(
            participant_id="participant",
            prompt="hello",
            config_path=Path("config.json"),
            approval="manual",
            **{option: value},
        )


@pytest.mark.parametrize("callback", ["launch", "source", "screen", "models", "resume"])
def test_callback_exceptions_propagate_without_masking(callback: str) -> None:
    error = RuntimeError(callback)

    def boom_launch(_context: LaunchContext) -> LaunchPlan:
        raise error

    def boom_source(_context: ParticipantObservationContext) -> Source:
        raise error

    def boom_screen(_context: ScreenContext) -> ScreenReading:
        raise error

    def boom_models(_context: ModelDiscoveryContext) -> tuple[str, ...]:
        raise error

    def boom_resume(_context: ResumeContext) -> ResumeLaunchOverlay:
        raise error

    built = manifest(
        launch=LaunchManifest(
            planner=boom_launch if callback == "launch" else plan,
            approvals=("manual",),
            supports_resume=callback == "resume",
            resume_planner=boom_resume if callback == "resume" else None,
        ),
        observation=ObservationManifest(
            primary=SourceManifest(
                factory=boom_source if callback == "source" else source,
                channel=primary_channel(),
            ),
            screen=ScreenManifest(classifier=boom_screen if callback == "screen" else screen),
        ),
        models=ModelDiscoveryManifest(discoverer=boom_models) if callback == "models" else None,
    )
    harness = compile_manifest("acme", built)

    with pytest.raises(RuntimeError) as raised:
        if callback == "launch":
            harness.plan_launch(
                participant_id="participant",
                prompt="",
                config_path=Path("config.json"),
                approval="manual",
            )
        elif callback == "source":
            harness.observer.open_source_context(
                ParticipantObservationContext(participant_id="participant", cwd=None)
            )
        elif callback == "screen":
            harness.observer.screen_reading("capture")
        elif callback == "models":
            harness.discover_models()
        else:
            predecessor = Participant(id="predecessor", transcript_domain=None)
            harness.resume_launch_overlay(
                predecessor=predecessor,
                trusted_session_owners=(predecessor,),
            )

    assert raised.value is error


@pytest.mark.parametrize("discovered", ["alpha", b"alpha", ("alpha", ""), ("alpha", 1), None])
def test_model_discovery_rejects_invalid_callback_output(discovered: object) -> None:
    def discoverer(_context: ModelDiscoveryContext) -> object:
        return discovered

    harness = compile_manifest(
        "acme",
        manifest(models=ModelDiscoveryManifest(discoverer=discoverer)),  # type: ignore[arg-type]
    )

    with pytest.raises(TypeError, match="model discoverer"):
        harness.discover_models()


def test_compiled_overlay_mcp_renders_through_the_declared_renderer(tmp_path: Path) -> None:
    """The overlay seam renders MCP onto an existing plan, not a new launch.

    A runtime backend plan reaches the same declared renderer ``plan_launch``
    uses — the argv/env/file merge and every validation — without a second
    launch-planner call.
    """
    planner_calls: list[LaunchContext] = []
    rendered: list[McpRenderContext] = []

    def launch_callback(context: LaunchContext) -> LaunchPlan:
        planner_calls.append(context)
        return LaunchPlan(argv=["acme", "backend"])

    def renderer(context: McpRenderContext) -> McpRenderOverlay:
        rendered.append(context)
        return McpRenderOverlay(
            argv=("--mcp", str(context.config_path)),
            env={"ACME_MCP": "1"},
            files={context.config_path: "{}"},
        )

    built = manifest(launch=LaunchManifest(planner=launch_callback, approvals=("manual",)))
    harness = compile_manifest("acme", replace(built, mcp=McpRenderingManifest(renderer=renderer)))
    config_path = tmp_path / "config.json"
    backend_plan = LaunchPlan(
        argv=["acme", "backend"],
        env={"KEEP": "yes"},
        secret_env={"ACME_PASSWORD": tmp_path / "runtime.token"},
    )

    overlay = harness.overlay_mcp(
        backend_plan,
        participant_id="participant",
        config_path=config_path,
        mcp_servers=(),
    )

    assert overlay.argv == ["acme", "backend", "--mcp", str(config_path)]
    assert overlay.env == {"KEEP": "yes", "ACME_MCP": "1"}
    assert overlay.files == {config_path: "{}"}
    assert overlay.private_files == backend_plan.private_files
    # A dropped credential leaves the backend on a password its runtime cannot know.
    assert overlay.secret_env == backend_plan.secret_env
    assert planner_calls == [], "overlay_mcp never calls the launch planner"
    assert [context.participant_id for context in rendered] == ["participant"]
    assert [context.plan for context in rendered] == [replace(backend_plan, secret_env={})]


def test_compiled_overlay_mcp_without_a_renderer_returns_the_plan_unchanged(
    tmp_path: Path,
) -> None:
    harness = compile_manifest("acme", manifest())
    assert harness.supports_mcp_rendering is False
    plan = LaunchPlan(argv=["acme"])
    assert (
        harness.overlay_mcp(
            plan,
            participant_id="p",
            config_path=tmp_path / "config.json",
            mcp_servers=(),
        )
        is plan
    )


def test_compilation_forwards_the_declared_foreground_wait() -> None:
    wait = McpForegroundWait(env="ACME_MCP_BACKGROUND_MS", default_ms=30_000, margin_seconds=2.5)
    declared = compile_manifest(
        "acme",
        replace(
            manifest(),
            mcp=McpRenderingManifest(
                renderer=lambda _context: McpRenderOverlay(), foreground_wait=wait
            ),
        ),
    )

    assert declared.mcp_foreground_wait is wait
    assert compile_manifest("acme", manifest()).mcp_foreground_wait is None
    # a zero margin and plain integers stay valid declarations
    assert (
        compile_manifest(
            "acme",
            replace(
                manifest(),
                mcp=McpRenderingManifest(
                    renderer=lambda _context: McpRenderOverlay(),
                    foreground_wait=McpForegroundWait(
                        env="ACME_MS", default_ms=1, margin_seconds=0
                    ),
                ),
            ),
        ).mcp_foreground_wait
        is not None
    )
