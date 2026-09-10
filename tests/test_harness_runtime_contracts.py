"""Frozen runtime contracts: manifest integration, validation, compatibility.

Covers the additive ``HarnessManifest.runtime`` field, manifest
validation/compilation of runtime declarations, the default-empty ``Batch``
terminal evidence, contract value bounds, the shared fake runtime used by
downstream workers, and the old-style local plugin that proves missing
runtime remains fully compatible.
"""

from __future__ import annotations

import ast
import importlib
import shutil
from pathlib import Path

import pytest

from tests.rig.fake_runtime import (
    FakeRuntime,
    completed_outcome,
    fake_runtime_context,
    fake_runtime_manifest,
)
from theater.harness.contracts.channels import (
    ChannelCapability,
    ChannelDeclaration,
    ChannelKind,
    SignalKind,
    SignalOwnership,
)
from theater.harness.contracts.harness import Harness
from theater.harness.contracts.launch import LaunchPlan
from theater.harness.contracts.manifest import (
    MANIFEST_API_VERSION,
    HarnessManifest,
    LaunchManifest,
    ObservationManifest,
    ScreenManifest,
)
from theater.harness.contracts.observation import ScreenKind, ScreenReading
from theater.harness.contracts.runtime import (
    CapabilityUnavailableReason,
    ConnectionHealth,
    ControlReceipt,
    DeliveryResult,
    LiveChannelDeclaration,
    NativeTurnOutcome,
    NativeTurnTerminal,
    RuntimeBinding,
    RuntimeCapabilities,
    RuntimeCapability,
    RuntimeCompatibility,
    RuntimeContext,
    RuntimeIO,
    RuntimeLifecyclePhase,
    RuntimeManifest,
    RuntimeSnapshot,
    RuntimeWiring,
    SessionOpenMode,
)
from theater.harness.contracts.source import Batch, Source, SourceContractError
from theater.harness.loading import LOCAL, scan
from theater.harness.manifests import ManifestValidationError, compile_manifest

OLD_STYLE_FIXTURE = Path(__file__).parent / "fixtures" / "plugins" / "oldstyle"


def _screen(capture: str) -> ScreenReading:
    return ScreenReading(kind=ScreenKind.PROMPT, confidence=0.5)


def _plan(context) -> LaunchPlan:
    return LaunchPlan(argv=[context.participant_id])


def _base_manifest(**overrides) -> HarnessManifest:
    manifest = HarnessManifest(
        api_version=MANIFEST_API_VERSION,
        binary="acme",
        icon="A",
        launch=LaunchManifest(planner=_plan, approvals=("manual",)),
        observation=ObservationManifest(primary=None, screen=ScreenManifest(classifier=_screen)),
    )
    import dataclasses

    return dataclasses.replace(manifest, **overrides)


def _live_channel(channel_id: str = "live") -> LiveChannelDeclaration:
    return LiveChannelDeclaration(
        channel=ChannelDeclaration(
            id=channel_id,
            kind=ChannelKind.LIVE,
            capabilities=(ChannelCapability(SignalKind.TURN, SignalOwnership.PRIMARY),),
        )
    )


def _runtime_manifest(channel: LiveChannelDeclaration | None = None) -> RuntimeManifest:
    return (
        fake_runtime_manifest()
        if channel is None
        else RuntimeManifest(
            probe=fake_runtime_manifest().probe,
            plan=fake_runtime_manifest().plan,
            factory=fake_runtime_manifest().factory,
            channel=channel,
        )
    )


# ---- manifest integration -------------------------------------------------


def test_runtime_defaults_to_none_and_compiles() -> None:
    manifest = _base_manifest()
    assert manifest.runtime is None
    harness = compile_manifest("acme", manifest)
    assert harness.runtime is None
    assert isinstance(harness, Harness)


def test_runtime_manifest_compiles_and_is_exposed() -> None:
    runtime = fake_runtime_manifest()
    harness = compile_manifest("acme", _base_manifest(runtime=runtime))
    assert harness.runtime is runtime
    assert harness.runtime.channel.channel.kind is ChannelKind.LIVE


def test_harness_base_class_runtime_annotation_defaults_none() -> None:
    assert Harness.runtime is None


def test_runtime_must_be_runtime_manifest() -> None:
    with pytest.raises(ManifestValidationError, match=r"manifest 'acme'\.runtime"):
        compile_manifest("acme", _base_manifest(runtime=object()))


def test_runtime_probe_must_be_callable() -> None:
    runtime = RuntimeManifest(
        probe=None,  # type: ignore[arg-type]
        plan=fake_runtime_manifest().plan,
        factory=fake_runtime_manifest().factory,
        channel=_live_channel(),
    )
    with pytest.raises(ManifestValidationError, match=r"runtime\.probe"):
        compile_manifest("acme", _base_manifest(runtime=runtime))


def test_runtime_channel_must_be_live_channel_declaration() -> None:
    runtime = RuntimeManifest(
        probe=fake_runtime_manifest().probe,
        plan=fake_runtime_manifest().plan,
        factory=fake_runtime_manifest().factory,
        channel=ChannelDeclaration(id="live", kind=ChannelKind.LIVE),  # type: ignore[arg-type]
    )
    with pytest.raises(ManifestValidationError, match=r"runtime\.channel"):
        compile_manifest("acme", _base_manifest(runtime=runtime))


def test_runtime_channel_id_may_not_duplicate_observation_channel() -> None:
    channel = _live_channel(channel_id="live")
    runtime = _runtime_manifest(channel=channel)
    manifest = _base_manifest(runtime=runtime)
    observation = manifest.observation
    import dataclasses

    observation = dataclasses.replace(
        observation,
        enrichments=(ChannelDeclaration(id="live", kind=ChannelKind.HOOK),),
    )
    manifest = dataclasses.replace(manifest, observation=observation)
    with pytest.raises(ManifestValidationError, match=r"runtime\.channel\.channel\.id"):
        compile_manifest("acme", manifest)


def test_live_channel_may_not_be_declared_as_enrichment() -> None:
    manifest = _base_manifest()
    import dataclasses

    observation = dataclasses.replace(
        manifest.observation,
        enrichments=(ChannelDeclaration(id="live", kind=ChannelKind.LIVE),),
    )
    manifest = dataclasses.replace(manifest, observation=observation)
    with pytest.raises(ManifestValidationError, match=r"declared by HarnessManifest\.runtime"):
        compile_manifest("acme", manifest)


# ---- Batch terminal evidence ------------------------------------------------


def test_batch_terminal_evidence_defaults_empty() -> None:
    batch = Batch()
    assert batch.terminal_evidence == ()


def test_existing_batch_constructors_stay_valid() -> None:
    batch = Batch(events=(), progressed=True, status=None)
    assert batch.terminal_evidence == ()


def test_batch_rejects_non_outcome_terminal_evidence() -> None:
    with pytest.raises(SourceContractError):
        Batch(terminal_evidence=[object()])  # type: ignore[list-item]


def test_batch_carries_native_terminal_evidence() -> None:
    outcome = NativeTurnOutcome(
        native_session_id="thread-1",
        native_turn_id="turn-1",
        terminal=NativeTurnTerminal.COMPLETED,
        result="done",
    )
    assert Batch(terminal_evidence=[outcome]).terminal_evidence == (outcome,)


# ---- Event native identity/revision ----------------------------------------


def test_event_native_identity_is_optional_and_backwards_compatible() -> None:
    from theater.harness.contracts.events import Event, EventKind

    legacy = Event(kind=EventKind.ASSISTANT, text="anonymous")
    assert legacy.native_id is None
    assert legacy.revision == 0
    identified = Event(kind=EventKind.ASSISTANT, text="identified", native_id="item-1", revision=3)
    assert identified.native_id == "item-1"
    assert identified.revision == 3


def test_event_native_identity_is_bounded() -> None:
    from theater.harness.contracts.events import Event, EventKind

    with pytest.raises(ValueError, match="native_id"):
        Event(kind=EventKind.USER, native_id=" ")
    with pytest.raises(ValueError, match="native_id"):
        Event(kind=EventKind.USER, native_id="x" * 513)
    assert Event(kind=EventKind.USER, native_id="x" * 512).native_id == "x" * 512


def test_event_revision_must_be_a_non_negative_integer() -> None:
    from theater.harness.contracts.events import Event, EventKind

    with pytest.raises(ValueError, match="revision"):
        Event(kind=EventKind.USER, revision=-1)
    with pytest.raises(ValueError, match="revision"):
        Event(kind=EventKind.USER, revision=True)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="revision"):
        Event(kind=EventKind.USER, revision="3")  # type: ignore[arg-type]
    assert Event(kind=EventKind.USER, revision=0).revision == 0


# ---- contract value bounds ----------------------------------------------------


def test_control_receipt_requires_bounded_fields() -> None:
    ok = ControlReceipt(operation_id="op-1", result=DeliveryResult.ACCEPTED)
    assert ok.result is DeliveryResult.ACCEPTED
    with pytest.raises(ValueError, match="operation_id"):
        ControlReceipt(operation_id=" ", result=DeliveryResult.ACCEPTED)
    with pytest.raises(TypeError, match="DeliveryResult"):
        ControlReceipt(operation_id="op-1", result="accepted")  # type: ignore[arg-type]


def test_native_turn_outcome_requires_exact_identity() -> None:
    with pytest.raises(ValueError, match="native_session_id"):
        NativeTurnOutcome(
            native_session_id="",
            native_turn_id="turn-1",
            terminal=NativeTurnTerminal.COMPLETED,
        )


def test_runtime_capabilities_fail_closed_by_default() -> None:
    capabilities = RuntimeCapabilities()
    for capability in RuntimeCapability:
        assert not capabilities.supports(capability)
        assert capabilities.reason_for(capability) is CapabilityUnavailableReason.NOT_DETERMINED


def test_default_snapshot_supports_nothing_before_determination() -> None:
    snapshot = RuntimeSnapshot(participant_id="p1", backend_generation=1)
    for capability in RuntimeCapability:
        assert not snapshot.capabilities.supports(capability)
        assert (
            snapshot.capabilities.reason_for(capability)
            is CapabilityUnavailableReason.NOT_DETERMINED
        )


def test_runtime_capabilities_report_explicit_reasons() -> None:
    capabilities = RuntimeCapabilities(
        available={RuntimeCapability.SEND, RuntimeCapability.INTERRUPT},
        unavailable_reasons={
            RuntimeCapability.SETTINGS_UPDATE: CapabilityUnavailableReason.GATED_BY_BACKEND
        },
    )
    assert capabilities.supports(RuntimeCapability.SEND)
    assert capabilities.reason_for(RuntimeCapability.SEND) is None
    assert not capabilities.supports(RuntimeCapability.SETTINGS_UPDATE)
    assert (
        capabilities.reason_for(RuntimeCapability.SETTINGS_UPDATE)
        is CapabilityUnavailableReason.GATED_BY_BACKEND
    )
    # Undetermined capabilities fail closed, not open.
    assert not capabilities.supports(RuntimeCapability.STEER)
    assert capabilities.reason_for(RuntimeCapability.STEER) is (
        CapabilityUnavailableReason.NOT_DETERMINED
    )
    with pytest.raises(ValueError, match="both available and unavailable"):
        RuntimeCapabilities(
            available={RuntimeCapability.SEND},
            unavailable_reasons={RuntimeCapability.SEND: CapabilityUnavailableReason.WIRING_MODE},
        )
    with pytest.raises(TypeError):
        RuntimeCapabilities(unavailable_reasons={"send": "nope"})  # type: ignore[dict-item]


def test_runtime_binding_binds_generation_not_cwd() -> None:
    binding = RuntimeBinding(
        participant_id="p1",
        backend_generation=3,
        wiring=RuntimeWiring.NATIVE,
        lifecycle=RuntimeLifecyclePhase.BOUND,
        native_session_id="thread-1",
        pid=4242,
    )
    assert binding.backend_generation == 3
    with pytest.raises(ValueError, match="pid"):
        RuntimeBinding(
            participant_id="p1",
            backend_generation=1,
            wiring=RuntimeWiring.NATIVE,
            pid=0,
        )


def test_runtime_compatibility_policy_is_bounded_name() -> None:
    assert RuntimeCompatibility(supported=True, policy="codex-0.154-verified").supported
    with pytest.raises(ValueError, match="policy"):
        RuntimeCompatibility(supported=True, policy="not a name!")


def test_snapshot_reports_pending_native_interaction_and_health() -> None:
    from theater.harness.contracts.runtime import NativeHumanInteraction, NativeInteractionKind

    snapshot = RuntimeSnapshot(
        participant_id="p1",
        backend_generation=1,
        native_session_id="thread-1",
        native_turn_id="turn-1",
        pending_interaction=NativeHumanInteraction(
            kind=NativeInteractionKind.APPROVAL,
            native_request_id="0",
        ),
        health=ConnectionHealth.CONNECTED,
    )
    assert snapshot.pending_interaction is not None
    assert snapshot.pending_interaction.kind is NativeInteractionKind.APPROVAL


def test_live_channel_declaration_must_wrap_live_kind() -> None:
    with pytest.raises(ValueError, match=r"ChannelKind\.LIVE"):
        LiveChannelDeclaration(channel=ChannelDeclaration(id="live", kind=ChannelKind.TRANSCRIPT))


def test_runtime_context_requires_injected_io() -> None:
    with pytest.raises(TypeError, match="RuntimeIO"):
        RuntimeContext(participant_id="p1", cwd=None, io=None, backend_generation=1)  # type: ignore[arg-type]


def test_runtime_context_binds_one_exact_backend_generation() -> None:
    io = fake_runtime_context("fake-1").io
    context = RuntimeContext(
        participant_id="p1",
        cwd=None,
        io=io,
        backend_generation=7,
        endpoint="unix:///tmp/sock",
    )
    assert context.backend_generation == 7
    with pytest.raises(TypeError, match="backend_generation"):
        RuntimeContext(participant_id="p1", cwd=None, io=io)  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="backend_generation"):
        RuntimeContext(participant_id="p1", cwd=None, io=io, backend_generation=-1)


# ---- the shared fake runtime ----------------------------------------------------


async def test_fake_runtime_open_session_and_frontend_plan() -> None:
    context = fake_runtime_context("fake-1")
    runtime = fake_runtime_manifest().factory(context)
    assert isinstance(runtime, FakeRuntime)
    binding = await runtime.open_session(mode=SessionOpenMode.NEW)
    assert binding.native_session_id is not None
    assert binding.lifecycle is RuntimeLifecyclePhase.BOUND

    plan = await runtime.frontend_plan(native_session_id=binding.native_session_id)
    joined = " ".join(plan.argv)
    assert binding.native_session_id in joined
    assert "prompt" not in joined


async def test_fake_runtime_ui_first_new_spawn_order() -> None:
    """The accepted new-spawn order: promptless UI before any session id."""
    context = fake_runtime_context("fake-1")
    runtime = fake_runtime_manifest().factory(context)

    # Before any session exists, the fresh plan is promptless.
    fresh = await runtime.frontend_plan(native_session_id=None)
    assert "resume" not in fresh.argv
    assert all("prompt" not in token.lower() for token in fresh.argv)

    # The freshly launched UI creates the session; open_session(NEW) then
    # opens exactly that UI-created session on the verified backend.
    binding = await runtime.open_session(mode=SessionOpenMode.NEW)
    assert binding.native_session_id is not None
    assert binding.backend_generation == context.backend_generation

    attach = await runtime.frontend_plan(native_session_id=binding.native_session_id)
    assert binding.native_session_id in attach.argv
    assert "resume" in attach.argv
    for plan in (fresh, attach):
        assert all("prompt" not in token.lower() for token in plan.argv)


async def test_fake_runtime_reconnect_orders_open_session_before_frontend_plan() -> None:
    context = fake_runtime_context("fake-1")
    runtime = fake_runtime_manifest().factory(context)
    # FORK/RECONNECT: open the exact session first, then plan the frontend
    # with the exact returned id.
    binding = await runtime.open_session(
        mode=SessionOpenMode.RECONNECT, native_session_id="thread-77"
    )
    assert binding.native_session_id == "thread-77"
    plan = await runtime.frontend_plan(native_session_id="thread-77")
    assert "thread-77" in plan.argv
    assert all("prompt" not in token.lower() for token in plan.argv)


def test_native_request_ids_match_the_captured_type() -> None:
    """Server request ids keep their captured type; ints stay ints."""
    from theater.harness.contracts.runtime import (
        NativeHumanInteraction,
        NativeInteractionKind,
        RuntimeNotification,
        validate_native_request_id,
    )

    # Wave 0 fixture: the server request id is the integer 0.
    interaction = NativeHumanInteraction(
        kind=NativeInteractionKind.APPROVAL,
        native_request_id=0,
    )
    assert interaction.native_request_id == 0
    assert isinstance(interaction.native_request_id, int)
    notification = RuntimeNotification(method="approval/request", request_id=0)
    assert notification.request_id == 0
    assert isinstance(notification.request_id, int)
    receipt = ControlReceipt(
        operation_id="op-1",
        result=DeliveryResult.ACCEPTED,
        native_request_id=7,
    )
    assert receipt.native_request_id == 7
    assert isinstance(receipt.native_request_id, int)

    # Optional correlation may be None; other natives use bounded strings.
    validate_native_request_id(None, "request id")
    validate_native_request_id("req-1", "request id")
    validate_native_request_id(2**63 - 1, "request id")


def test_native_request_ids_reject_mismatched_values() -> None:
    from theater.harness.contracts.runtime import (
        NativeHumanInteraction,
        NativeInteractionKind,
        RuntimeNotification,
        validate_native_request_id,
    )

    with pytest.raises(TypeError, match="bool"):
        validate_native_request_id(True, "request id")
    with pytest.raises(ValueError, match="non-negative bounded integer"):
        validate_native_request_id(-1, "request id")
    with pytest.raises(ValueError, match="non-negative bounded integer"):
        validate_native_request_id(2**63, "request id")
    with pytest.raises(ValueError, match="bounded"):
        validate_native_request_id("  ", "request id")
    with pytest.raises(ValueError, match="bounded"):
        validate_native_request_id("x" * 1024, "request id")
    # The validator is applied by the contract constructors themselves.
    with pytest.raises(TypeError, match="bool"):
        NativeHumanInteraction(
            kind=NativeInteractionKind.APPROVAL,
            native_request_id=False,
        )
    with pytest.raises(ValueError, match="request_id"):
        RuntimeNotification(method="approval/request", request_id="")


async def test_fake_runtime_shared_live_source_and_controls() -> None:
    context = fake_runtime_context("fake-1")
    runtime = fake_runtime_manifest().factory(context)
    await runtime.open_session(mode=SessionOpenMode.NEW)
    source = runtime.live_source()
    assert isinstance(source, Source)
    assert runtime.live_source() is source

    receipt = await runtime.send(operation_id="op-1", prompt="do the thing")
    assert receipt.result is DeliveryResult.ACCEPTED
    assert receipt.native_turn_id is not None

    steer = await runtime.steer(
        operation_id="op-2", native_turn_id=receipt.native_turn_id, prompt="more"
    )
    assert steer.result is DeliveryResult.ACCEPTED

    stale = await runtime.steer(operation_id="op-3", native_turn_id="turn-999", prompt="nope")
    assert stale.result is DeliveryResult.REJECTED
    assert stale.error_code == "stale_turn"

    interrupt = await runtime.interrupt(operation_id="op-4")
    assert interrupt.result is DeliveryResult.ACCEPTED

    settings = await runtime.update_settings(operation_id="op-5", model="m1")
    assert settings.result is DeliveryResult.ACCEPTED
    snapshot = await runtime.snapshot()
    assert snapshot.settings.model == "m1"
    assert snapshot.health is ConnectionHealth.CONNECTED


async def test_fake_runtime_aclose_disconnects_without_terminating() -> None:
    context = fake_runtime_context("fake-1")
    runtime = fake_runtime_manifest().factory(context)
    await runtime.open_session(mode=SessionOpenMode.NEW)
    await runtime.aclose()
    assert not runtime.state.connected
    assert runtime.state.closed
    # The backend must survive Theater disconnecting.
    assert runtime.state.backend_alive


async def test_fake_runtime_terminal_evidence_flows_through_batch() -> None:
    context = fake_runtime_context("fake-1")
    runtime = fake_runtime_manifest().factory(context)
    await runtime.open_session(mode=SessionOpenMode.NEW)
    await runtime.send(operation_id="op-1", prompt="work")
    outcome = completed_outcome(runtime.state)
    runtime.state.batches.append(Batch(terminal_evidence=[outcome]))
    batch = await runtime.live_source().read()
    assert batch.terminal_evidence == (outcome,)


async def test_fake_runtime_settings_capability_gated() -> None:
    from theater.harness.contracts.runtime import RuntimeCapability

    context = fake_runtime_context("fake-1")
    runtime = fake_runtime_manifest().factory(context)
    runtime.state.unavailable[RuntimeCapability.SETTINGS_UPDATE] = (
        CapabilityUnavailableReason.GATED_BY_BACKEND
    )
    receipt = await runtime.update_settings(operation_id="op-1", model="m1")
    assert receipt.result is DeliveryResult.REJECTED
    assert receipt.error_code == "settings_unavailable"


async def test_fake_runtime_snapshot_capabilities_are_explicitly_enabled() -> None:
    from theater.harness.contracts.runtime import RuntimeCapability

    context = fake_runtime_context("fake-1")
    runtime = fake_runtime_manifest().factory(context)
    await runtime.open_session(mode=SessionOpenMode.NEW)
    snapshot = await runtime.snapshot()
    for capability in RuntimeCapability:
        assert snapshot.capabilities.supports(capability)
        assert snapshot.capabilities.reason_for(capability) is None

    runtime.state.unavailable[RuntimeCapability.SETTINGS_UPDATE] = (
        CapabilityUnavailableReason.GATED_BY_BACKEND
    )
    snapshot = await runtime.snapshot()
    assert not snapshot.capabilities.supports(RuntimeCapability.SETTINGS_UPDATE)
    assert snapshot.capabilities.reason_for(RuntimeCapability.SETTINGS_UPDATE) is (
        CapabilityUnavailableReason.GATED_BY_BACKEND
    )
    # Explicitly enabled capabilities keep working alongside a gated one.
    assert snapshot.capabilities.supports(RuntimeCapability.SEND)


# ---- old-style local plugin compatibility --------------------------------------


def test_old_style_plugin_without_runtime_stays_compatible(theater_home) -> None:
    plugins = theater_home / "plugins"
    plugins.mkdir(parents=True, exist_ok=True)
    shutil.copytree(OLD_STYLE_FIXTURE, plugins / "oldstyle")
    loaded = [plugin for plugin in scan(plugins, source=LOCAL) if not plugin.error]
    names = [plugin.name for plugin in loaded]
    assert "oldstyle" in names

    manifest = loaded[names.index("oldstyle")].manifest
    assert isinstance(manifest, HarnessManifest)
    assert manifest.runtime is None

    harness = compile_manifest("oldstyle", manifest)
    assert harness.runtime is None
    plan = harness.plan_launch(
        participant_id="p1",
        prompt="hello",
        config_path=Path("/tmp/oldstyle-mcp.json"),
        approval="manual",
    )
    assert plan.argv[0] == "acme"
    assert "hello" in plan.argv
    reading = harness.observer.screen_reading("ready ❯")
    assert reading.kind is ScreenKind.PROMPT


def test_registry_installs_old_style_plugin_alongside_builtin(theater_home, tmp_path) -> None:
    plugins = theater_home / "plugins"
    plugins.mkdir(parents=True, exist_ok=True)
    shutil.copytree(OLD_STYLE_FIXTURE, plugins / "oldstyle")
    from theater import config as cfg
    from theater import harness as harness_registry

    names = harness_registry.install(cfg.Config(), local_dir=plugins)
    assert "oldstyle" in names
    assert harness_registry.get("oldstyle").runtime is None


# ---- import boundary ---------------------------------------------------------------


def test_runtime_contracts_do_not_import_daemon_internals() -> None:
    module = importlib.import_module("theater.harness.contracts.runtime")
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ImportFrom)
            and node.module
            and node.module.startswith("theater.daemon")
        ):
            pytest.fail("runtime contracts must not import theater.daemon")
    assert RuntimeIO is not None
