"""Focused contracts for immutable harness manifests and their compiler."""

from __future__ import annotations

import ast
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from theater.constants.harness import (
    HARNESS_CHANNEL_HEALTH_DIAGNOSTIC_MAX_CHARS,
    HARNESS_CHANNEL_HEALTH_MAX_DIAGNOSTICS,
    HARNESS_CHANNEL_ID_MAX_CHARS,
)
from theater.harness.contracts.callbacks import (
    LaunchContext,
    ModelDiscoveryContext,
    ResumeContext,
    ScreenContext,
)
from theater.harness.contracts.channels import (
    ChannelBounds,
    ChannelCapability,
    ChannelDeclaration,
    ChannelHealth,
    ChannelHealthState,
    ChannelKind,
    SignalKind,
    SignalOwnership,
)
from theater.harness.contracts.context import ParticipantObservationContext
from theater.harness.contracts.harness import Harness, LaunchParameterSupport
from theater.harness.contracts.launch import LaunchPlan, ResumeLaunchOverlay
from theater.harness.contracts.manifest import (
    MANIFEST_API_VERSION,
    HarnessManifest,
    IdentityManifest,
    LaunchManifest,
    LineageManifest,
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
from theater.harness.contracts.source import Batch, Source
from theater.harness.manifests.compiler import compile_manifest
from theater.harness.manifests.strategies import ScreenMarker, screen_classifier_from_markers
from theater.harness.manifests.validation import ManifestValidationError
from theater.harness.registry.capabilities import (
    supports_model,
    supports_reasoning,
    supports_resume,
)
from theater.models import BadRequest, Participant
from theater.provenance import TranscriptProvenance


class StubSource(Source):
    async def read(self) -> Batch:
        return Batch()


def plan(context: LaunchContext) -> LaunchPlan:
    return LaunchPlan(argv=["acme", context.participant_id])


def source(_context: ParticipantObservationContext) -> Source:
    return StubSource()


def screen(_context: ScreenContext) -> ScreenReading:
    return ScreenReading(ScreenKind.PROMPT, ScreenConfidence.HIGH)


def primary_channel(
    capabilities: tuple[ChannelCapability, ...] = (),
    *,
    channel_id: str = "primary",
) -> ChannelDeclaration:
    return ChannelDeclaration(
        id=channel_id,
        kind=ChannelKind.TRANSCRIPT,
        capabilities=capabilities,
    )


def manifest(
    *,
    launch: LaunchManifest | None = None,
    observation: ObservationManifest | None = None,
    models: ModelDiscoveryManifest | None = None,
    aliases: tuple[str, ...] = (),
    binaries: frozenset[str] = frozenset(),
) -> HarnessManifest:
    return HarnessManifest(
        api_version=MANIFEST_API_VERSION,
        binary="acme",
        icon="@",
        binaries=binaries,
        aliases=aliases,
        launch=launch
        or LaunchManifest(
            planner=plan,
            approvals=frozenset({"manual", "edits", "yolo"}),
        ),
        observation=observation
        or ObservationManifest(
            primary=SourceManifest(factory=source, channel=primary_channel()),
            screen=ScreenManifest(classifier=screen),
        ),
        models=models,
    )


def test_values_are_frozen_and_copy_collection_inputs() -> None:
    aliases = ["acme-cli"]
    binaries = {"acme-alt"}
    capabilities = [ChannelCapability(SignalKind.CONTENT, SignalOwnership.PRIMARY)]
    enrichments = [
        ChannelDeclaration(
            id="hook",
            kind=ChannelKind.HOOK,
            capabilities=[ChannelCapability(SignalKind.TOOL, SignalOwnership.ENRICHMENT)],
        )
    ]
    diagnostics = ["ready"]
    built = manifest(
        aliases=aliases,
        binaries=binaries,
        observation=ObservationManifest(
            primary=SourceManifest(
                factory=source,
                channel=primary_channel(capabilities),
            ),
            screen=ScreenManifest(classifier=screen),
            enrichments=enrichments,
        ),
    )
    health = ChannelHealth(
        channel_id="hook",
        state=ChannelHealthState.HEALTHY,
        diagnostics=diagnostics,
    )

    aliases.append("later")
    binaries.add("later")
    capabilities.append(ChannelCapability(SignalKind.USAGE, SignalOwnership.PRIMARY))
    enrichments.append(ChannelDeclaration(id="later", kind=ChannelKind.HOOK))
    diagnostics.append("later")

    assert built.aliases == ("acme-cli",)
    assert built.binaries == frozenset({"acme-alt"})
    assert built.observation.primary is not None
    assert built.observation.primary.channel.capabilities == (
        ChannelCapability(SignalKind.CONTENT, SignalOwnership.PRIMARY),
    )
    assert tuple(channel.id for channel in built.observation.enrichments) == ("hook",)
    assert health.diagnostics == ("ready",)
    with pytest.raises(FrozenInstanceError):
        built.binary = "other"  # type: ignore[misc]
    with pytest.raises(AttributeError):
        built.aliases.append("other")


def test_channel_capability_requires_explicit_ownership() -> None:
    with pytest.raises(TypeError):
        ChannelCapability(SignalKind.CONTENT)  # type: ignore[call-arg]


def test_primary_source_declares_its_durable_kind_explicitly() -> None:
    compiled = compile_manifest(
        "acme",
        manifest(
            observation=ObservationManifest(
                primary=SourceManifest(
                    factory=source,
                    channel=ChannelDeclaration(id="database", kind=ChannelKind.DATABASE),
                ),
                screen=ScreenManifest(classifier=screen),
            )
        ),
    )

    assert isinstance(compiled, Harness)


@pytest.mark.parametrize("name", ["Acme", "-acme", ""])
def test_canonical_name_validation_is_path_qualified(name: str) -> None:
    with pytest.raises(ManifestValidationError) as raised:
        compile_manifest(name, manifest())

    assert raised.value.path == "name"
    assert "manifest" in str(raised.value)


@pytest.mark.parametrize(
    ("changed", "path"),
    [
        (lambda built: replace(built, binary=" "), "binary"),
        (lambda built: replace(built, icon="@@"), "icon"),
        (lambda built: replace(built, aliases="alias"), "aliases"),
        (lambda built: replace(built, aliases=("same", "same")), "aliases[1]"),
        (lambda built: replace(built, binaries="acme-alt"), "binaries"),
        (lambda built: replace(built, binaries=frozenset({"acme"})), "binaries"),
    ],
)
def test_metadata_validation_is_path_qualified(changed, path: str) -> None:
    with pytest.raises(ManifestValidationError) as raised:
        compile_manifest("acme", changed(manifest()))

    assert raised.value.path == path


def test_unsupported_api_version_is_rejected() -> None:
    with pytest.raises(ManifestValidationError) as raised:
        compile_manifest("acme", replace(manifest(), api_version=MANIFEST_API_VERSION + 1))

    assert raised.value.path == "api_version"
    assert "unsupported API version" in str(raised.value)


@pytest.mark.parametrize(
    ("changed", "path"),
    [
        (
            lambda built: replace(
                built,
                launch=LaunchManifest(planner=None, approvals=frozenset({"manual"})),  # type: ignore[arg-type]
            ),
            "launch.planner",
        ),
        (
            lambda built: replace(
                built,
                observation=replace(
                    built.observation,
                    primary=SourceManifest(
                        factory="not-a-callback",  # type: ignore[arg-type]
                        channel=primary_channel(),
                    ),
                ),
            ),
            "observation.primary.factory",
        ),
        (
            lambda built: replace(
                built,
                observation=replace(
                    built.observation,
                    screen=ScreenManifest(classifier=None),  # type: ignore[arg-type]
                ),
            ),
            "observation.screen.classifier",
        ),
    ],
)
def test_missing_or_wrong_callback_diagnostics(changed, path: str) -> None:
    with pytest.raises(ManifestValidationError) as raised:
        compile_manifest("acme", changed(manifest()))

    assert (raised.value.path, raised.value.message) == (path, "must be callable")


@pytest.mark.parametrize(
    ("launch", "path"),
    [
        (
            LaunchManifest(
                planner=plan,
                approvals=frozenset({"manual"}),
                resume_planner=lambda _context: ResumeLaunchOverlay(),
            ),
            "launch.resume_planner",
        ),
        (
            LaunchManifest(
                planner=plan,
                approvals=frozenset({"manual"}),
                resume_takes_prompt=False,
            ),
            "launch.resume_takes_prompt",
        ),
        (
            LaunchManifest(
                planner=plan,
                approvals=frozenset({"manual"}),
                resume_strategy="fork",
            ),
            "launch.resume_strategy",
        ),
    ],
)
def test_resume_configuration_requires_resume_support(launch: LaunchManifest, path: str) -> None:
    with pytest.raises(ManifestValidationError) as raised:
        compile_manifest("acme", manifest(launch=launch))

    assert raised.value.path == path


def test_compilation_forwards_typed_callbacks_and_runtime_contracts(tmp_path: Path) -> None:
    seen_launch: list[LaunchContext] = []
    seen_source: list[ParticipantObservationContext] = []
    seen_screen: list[ScreenContext] = []
    seen_models: list[ModelDiscoveryContext] = []
    seen_resume: list[ResumeContext] = []
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

    built = manifest(
        launch=LaunchManifest(
            planner=launch_callback,
            approvals=frozenset({"manual", "edits"}),
            supports_model=True,
            supports_reasoning_effort=True,
            supports_resume=True,
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
        )
    ]
    assert seen_source == [context]
    assert seen_screen == [ScreenContext(capture="ready> ")]
    assert seen_models == [ModelDiscoveryContext(name="acme", binary="acme")]
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
                approvals=frozenset({"manual"}),
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
            approvals=frozenset({"manual"}),
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


def test_channel_duplicate_ids_conflicts_and_explicit_fallbacks() -> None:
    primary = primary_channel((ChannelCapability(SignalKind.CONTENT, SignalOwnership.PRIMARY),))
    duplicate = ChannelDeclaration(id="primary", kind=ChannelKind.HOOK)
    conflict = ChannelDeclaration(
        id="hook",
        kind=ChannelKind.HOOK,
        capabilities=(ChannelCapability(SignalKind.CONTENT, SignalOwnership.ENRICHMENT),),
    )
    fallback = ChannelDeclaration(
        id="hook",
        kind=ChannelKind.HOOK,
        capabilities=(ChannelCapability(SignalKind.CONTENT, SignalOwnership.FALLBACK),),
    )

    with pytest.raises(ManifestValidationError) as duplicate_error:
        compile_manifest(
            "acme",
            manifest(
                observation=ObservationManifest(
                    primary=SourceManifest(factory=source, channel=primary),
                    screen=ScreenManifest(classifier=screen),
                    enrichments=(duplicate,),
                )
            ),
        )
    assert duplicate_error.value.path == "observation.enrichments[0].id"

    with pytest.raises(ManifestValidationError) as ownership_error:
        compile_manifest(
            "acme",
            manifest(
                observation=ObservationManifest(
                    primary=SourceManifest(factory=source, channel=primary),
                    screen=ScreenManifest(classifier=screen),
                    enrichments=(conflict,),
                )
            ),
        )
    assert ownership_error.value.path.endswith("ownership")
    assert "mark one owner as fallback" in str(ownership_error.value)

    compiled = compile_manifest(
        "acme",
        manifest(
            observation=ObservationManifest(
                primary=SourceManifest(factory=source, channel=primary),
                screen=ScreenManifest(classifier=screen),
                enrichments=(fallback,),
            )
        ),
    )
    assert isinstance(compiled, Harness)


def test_channel_validation_checks_duplicate_signals_bounds_and_identifier_size() -> None:
    duplicate_signal = ChannelDeclaration(
        id="hook",
        kind=ChannelKind.HOOK,
        capabilities=(
            ChannelCapability(SignalKind.CONTENT, SignalOwnership.PRIMARY),
            ChannelCapability(SignalKind.CONTENT, SignalOwnership.FALLBACK),
        ),
    )
    zero_bound = ChannelDeclaration(
        id="hook",
        kind=ChannelKind.HOOK,
        bounds=ChannelBounds(max_queue=0),
    )
    oversized_id = ChannelDeclaration(
        id="a" * (HARNESS_CHANNEL_ID_MAX_CHARS + 1),
        kind=ChannelKind.HOOK,
    )

    for channel, expected_path in (
        (duplicate_signal, "capabilities[1].signal"),
        (zero_bound, "bounds.max_queue"),
        (oversized_id, "id"),
    ):
        with pytest.raises(ManifestValidationError) as raised:
            compile_manifest(
                "acme",
                manifest(
                    observation=ObservationManifest(
                        primary=SourceManifest(factory=source, channel=primary_channel()),
                        screen=ScreenManifest(classifier=screen),
                        enrichments=(channel,),
                    )
                ),
            )
        assert raised.value.path.endswith(expected_path)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"channel_id": ""},
        {"channel_id": "Hook"},
        {"channel_id": "a" * (HARNESS_CHANNEL_ID_MAX_CHARS + 1)},
        {"channel_id": "hook", "diagnostics": (" ",)},
        {
            "channel_id": "hook",
            "diagnostics": ("x" * (HARNESS_CHANNEL_HEALTH_DIAGNOSTIC_MAX_CHARS + 1),),
        },
        {
            "channel_id": "hook",
            "diagnostics": ("x",) * (HARNESS_CHANNEL_HEALTH_MAX_DIAGNOSTICS + 1),
        },
        {"channel_id": "hook", "dropped": -1},
    ],
)
def test_channel_health_rejects_invalid_bounded_values(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        ChannelHealth(  # type: ignore[arg-type]
            channel_id=kwargs.get("channel_id", "hook"),
            diagnostics=kwargs.get("diagnostics", ()),
            dropped=kwargs.get("dropped", 0),
        )


def test_screen_marker_strategy_is_ordered() -> None:
    classifier = screen_classifier_from_markers(
        (
            ScreenMarker("approval", ScreenReading(ScreenKind.APPROVAL)),
            ScreenMarker("prompt", ScreenReading(ScreenKind.PROMPT)),
        )
    )

    assert classifier(ScreenContext("approval prompt")).kind is ScreenKind.APPROVAL


@pytest.mark.parametrize(
    ("identity_callback", "path"),
    [
        ("stream_floor", "observation.identity.stream_floor"),
        ("transcript_candidates", "observation.identity.transcript_candidates"),
        ("receipt_validator", "observation.identity.receipt_validator"),
        ("operator_candidate_admitter", "observation.identity.operator_candidate_admitter"),
    ],
)
def test_identity_callback_without_primary_source_is_rejected(
    identity_callback: str, path: str
) -> None:
    identity = IdentityManifest(**{identity_callback: lambda _ctx: None})  # type: ignore[arg-type]
    with pytest.raises(ManifestValidationError) as raised:
        compile_manifest(
            "acme",
            manifest(
                observation=ObservationManifest(
                    primary=None,
                    screen=ScreenManifest(classifier=screen),
                    identity=identity,
                )
            ),
        )
    assert raised.value.path == path
    assert "requires a primary source" in raised.value.message


def test_lineage_callback_without_primary_source_is_rejected() -> None:
    lineage = LineageManifest(native_children=lambda _ctx: [])
    with pytest.raises(ManifestValidationError) as raised:
        compile_manifest(
            "acme",
            manifest(
                observation=ObservationManifest(
                    primary=None,
                    screen=ScreenManifest(classifier=screen),
                    lineage=lineage,
                )
            ),
        )
    assert raised.value.path == "observation.lineage.native_children"
    assert "requires a primary source" in raised.value.message


@pytest.mark.parametrize(
    ("identity_callback", "path"),
    [
        ("stream_floor", "observation.identity.stream_floor"),
        ("transcript_candidates", "observation.identity.transcript_candidates"),
        ("receipt_validator", "observation.identity.receipt_validator"),
        ("operator_candidate_admitter", "observation.identity.operator_candidate_admitter"),
    ],
)
def test_non_callable_identity_callbacks_are_rejected(identity_callback: str, path: str) -> None:
    identity = IdentityManifest(**{identity_callback: "not-callable"})  # type: ignore[arg-type]
    with pytest.raises(ManifestValidationError) as raised:
        compile_manifest(
            "acme",
            manifest(
                observation=ObservationManifest(
                    primary=SourceManifest(factory=source, channel=primary_channel()),
                    screen=ScreenManifest(classifier=screen),
                    identity=identity,
                )
            ),
        )
    assert raised.value.path == path
    assert raised.value.message == "must be callable or null"


def test_non_callable_lineage_callback_is_rejected() -> None:
    lineage = LineageManifest(native_children="not-callable")  # type: ignore[arg-type]
    with pytest.raises(ManifestValidationError) as raised:
        compile_manifest(
            "acme",
            manifest(
                observation=ObservationManifest(
                    primary=SourceManifest(factory=source, channel=primary_channel()),
                    screen=ScreenManifest(classifier=screen),
                    lineage=lineage,
                )
            ),
        )
    assert raised.value.path == "observation.lineage.native_children"
    assert raised.value.message == "must be callable or null"


def test_wrong_identity_manifest_type_is_rejected() -> None:
    with pytest.raises(ManifestValidationError) as raised:
        compile_manifest(
            "acme",
            manifest(
                observation=ObservationManifest(
                    primary=SourceManifest(factory=source, channel=primary_channel()),
                    screen=ScreenManifest(classifier=screen),
                    identity="not-a-manifest",  # type: ignore[arg-type]
                )
            ),
        )
    assert raised.value.path == "observation.identity"
    assert "expected IdentityManifest" in raised.value.message


def test_wrong_lineage_manifest_type_is_rejected() -> None:
    with pytest.raises(ManifestValidationError) as raised:
        compile_manifest(
            "acme",
            manifest(
                observation=ObservationManifest(
                    primary=SourceManifest(factory=source, channel=primary_channel()),
                    screen=ScreenManifest(classifier=screen),
                    lineage="not-a-manifest",  # type: ignore[arg-type]
                )
            ),
        )
    assert raised.value.path == "observation.lineage"
    assert "expected LineageManifest" in raised.value.message


def test_wrong_trajectory_capabilities_type_is_rejected() -> None:
    with pytest.raises(ManifestValidationError) as raised:
        compile_manifest(
            "acme",
            manifest(
                observation=ObservationManifest(
                    primary=SourceManifest(factory=source, channel=primary_channel()),
                    screen=ScreenManifest(classifier=screen),
                    trajectory_capabilities="not-capabilities",  # type: ignore[arg-type]
                )
            ),
        )
    assert raised.value.path == "observation.trajectory_capabilities"
    assert "expected TrajectoryCapabilities" in raised.value.message


def test_screen_only_manifest_without_callbacks_is_valid() -> None:
    compiled = compile_manifest(
        "acme",
        manifest(
            observation=ObservationManifest(
                primary=None,
                screen=ScreenManifest(classifier=screen),
            )
        ),
    )
    assert isinstance(compiled, Harness)
    assert compiled.observer.has_transcript is False


def test_contract_and_compiler_modules_do_not_import_runtime_layers() -> None:
    root = Path(__file__).parents[1]
    files = (
        root / "theater/harness/contracts/manifest.py",
        root / "theater/harness/contracts/callbacks.py",
        root / "theater/harness/contracts/channels.py",
        root / "theater/harness/manifests/compiler.py",
        root / "theater/harness/manifests/validation.py",
        root / "theater/harness/manifests/strategies.py",
    )
    forbidden = (
        "theater.daemon",
        "theater.regie",
        "theater.tmux",
        "theater.harness.builtin",
    )

    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module is not None
        }
        imported.update(
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        )
        assert not any(name.startswith(forbidden) for name in imported), path
