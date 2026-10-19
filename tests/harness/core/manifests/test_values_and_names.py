"""Manifest values are frozen and validated: names, metadata, approval, callbacks and resume."""

from __future__ import annotations

import math
from dataclasses import FrozenInstanceError, replace
from functools import partial

import pytest

from tests.support.manifest_rig import manifest, plan, primary_channel, screen, source
from tests.support.tables import run_rows
from theater.harness.contracts.channels import (
    ChannelCapability,
    ChannelDeclaration,
    ChannelHealth,
    ChannelHealthState,
    ChannelKind,
    SignalKind,
    SignalOwnership,
)
from theater.harness.contracts.harness import Harness
from theater.harness.contracts.launch import McpRenderOverlay, ResumeLaunchOverlay
from theater.harness.contracts.manifest import (
    MANIFEST_API_VERSION,
    ControlManifest,
    InterruptPlan,
    LaunchManifest,
    McpForegroundWait,
    McpRenderingManifest,
    ObservationManifest,
    ScreenManifest,
    SourceManifest,
)
from theater.harness.manifests.compiler import compile_manifest
from theater.harness.manifests.validation import ManifestValidationError


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


def test_interrupt_controls_are_frozen_and_compile_to_the_runtime() -> None:
    keys = ["Escape"]
    controls = ControlManifest(interrupt=InterruptPlan(keys=keys))
    keys.append("Enter")

    compiled = compile_manifest("acme", manifest(controls=controls))

    assert controls.interrupt is not None
    assert controls.interrupt.keys == ("Escape",)
    assert compiled.controls is controls
    with pytest.raises(FrozenInstanceError):
        controls.interrupt.keys = ("Enter",)  # type: ignore[misc]


def test_interrupt_controls_are_validated() -> None:
    """Every malformed interrupt plan names the offending path."""
    cases = [
        ("not_controls", "not-controls", "controls"),
        ("not_a_plan", ControlManifest(interrupt="not-a-plan"), "controls.interrupt"),
        (
            "empty_keys",
            ControlManifest(interrupt=InterruptPlan(keys=())),
            "controls.interrupt.keys",
        ),
        (
            "string_keys",
            ControlManifest(interrupt=InterruptPlan(keys="Escape")),
            "controls.interrupt.keys",
        ),
        (
            "too_many_keys",
            ControlManifest(interrupt=InterruptPlan(keys=("Escape",) * 5)),
            "controls.interrupt.keys",
        ),
        (
            "long_option",
            ControlManifest(interrupt=InterruptPlan(keys=("--option",))),
            "controls.interrupt.keys[0]",
        ),
        (
            "negative_delay",
            ControlManifest(
                interrupt=InterruptPlan(keys=("Escape",), inter_key_delay_seconds=-0.01)
            ),
            "controls.interrupt.inter_key_delay_seconds",
        ),
        (
            "delay_over_one",
            ControlManifest(
                interrupt=InterruptPlan(keys=("Escape",), inter_key_delay_seconds=1.01)
            ),
            "controls.interrupt.inter_key_delay_seconds",
        ),
    ]

    def rejects(controls: object, path: str) -> None:
        with pytest.raises(ManifestValidationError) as raised:
            compile_manifest("acme", manifest(controls=controls))  # type: ignore[arg-type]
        assert raised.value.path == path

    run_rows((label, partial(rejects, controls, path)) for label, controls, path in cases)


def test_shipped_interrupt_plans_match_native_key_handlers() -> None:
    from theater.harness.builtin.plugins.claude.manifest import MANIFEST as CLAUDE_MANIFEST
    from theater.harness.builtin.plugins.codex.manifest import MANIFEST as CODEX_MANIFEST
    from theater.harness.builtin.plugins.opencode.manifest import MANIFEST as OPENCODE_MANIFEST
    from theater.harness.builtin.plugins.vibe.manifest import MANIFEST as VIBE_MANIFEST

    assert {
        "claude": CLAUDE_MANIFEST.controls.interrupt,
        "codex": CODEX_MANIFEST.controls.interrupt,
        "opencode": OPENCODE_MANIFEST.controls.interrupt,
        "vibe": VIBE_MANIFEST.controls.interrupt,
    } == {
        "claude": InterruptPlan(keys=("Escape",)),
        "codex": InterruptPlan(keys=("Escape",)),
        "opencode": InterruptPlan(keys=("Escape", "Escape"), inter_key_delay_seconds=0.05),
        "vibe": InterruptPlan(keys=("Escape",)),
    }


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


@pytest.mark.parametrize("approval", [[], object()])
def test_invalid_approval_values_get_manifest_diagnostics(approval: object) -> None:
    launch = LaunchManifest(planner=plan, approvals=(approval,))  # type: ignore[arg-type]

    with pytest.raises(ManifestValidationError) as raised:
        compile_manifest("acme", manifest(launch=launch))

    assert raised.value.path == "launch.approvals"
    assert "expected only manual, edits, yolo" in raised.value.message


@pytest.mark.parametrize(
    ("changed", "path"),
    [
        (
            lambda built: replace(
                built,
                launch=LaunchManifest(planner=None, approvals=("manual",)),  # type: ignore[arg-type]
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
                approvals=("manual",),
                resume_preflight=lambda _context: None,
            ),
            "launch.resume_preflight",
        ),
        (
            LaunchManifest(
                planner=plan,
                approvals=("manual",),
                resume_planner=lambda _context: ResumeLaunchOverlay(),
            ),
            "launch.resume_planner",
        ),
        (
            LaunchManifest(
                planner=plan,
                approvals=("manual",),
                resume_takes_prompt=False,
            ),
            "launch.resume_takes_prompt",
        ),
        (
            LaunchManifest(
                planner=plan,
                approvals=("manual",),
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


@pytest.mark.parametrize(
    ("wait", "path"),
    [
        ("not-a-wait", "mcp.foreground_wait"),
        (
            McpForegroundWait(env=" ", default_ms=30_000, margin_seconds=2.5),
            "mcp.foreground_wait.env",
        ),
        (
            McpForegroundWait(env="ACME_MS", default_ms=0, margin_seconds=2.5),
            "mcp.foreground_wait.default_ms",
        ),
        (
            McpForegroundWait(env="ACME_MS", default_ms=-1, margin_seconds=2.5),
            "mcp.foreground_wait.default_ms",
        ),
        (
            McpForegroundWait(env="ACME_MS", default_ms=True, margin_seconds=2.5),
            "mcp.foreground_wait.default_ms",
        ),
        (
            McpForegroundWait(env="ACME_MS", default_ms=math.inf, margin_seconds=2.5),
            "mcp.foreground_wait.default_ms",
        ),
        (
            McpForegroundWait(env="ACME_MS", default_ms=math.nan, margin_seconds=2.5),
            "mcp.foreground_wait.default_ms",
        ),
        (
            McpForegroundWait(env="ACME_MS", default_ms="30000", margin_seconds=2.5),
            "mcp.foreground_wait.default_ms",
        ),
        (
            McpForegroundWait(env="ACME_MS", default_ms=30_000, margin_seconds=-0.1),
            "mcp.foreground_wait.margin_seconds",
        ),
        (
            McpForegroundWait(env="ACME_MS", default_ms=30_000, margin_seconds=True),
            "mcp.foreground_wait.margin_seconds",
        ),
        (
            McpForegroundWait(env="ACME_MS", default_ms=30_000, margin_seconds=math.inf),
            "mcp.foreground_wait.margin_seconds",
        ),
    ],
)
def test_foreground_wait_declarations_are_validated(wait: object, path: str) -> None:
    built = replace(
        manifest(),
        mcp=McpRenderingManifest(
            renderer=lambda _context: McpRenderOverlay(),
            foreground_wait=wait,  # type: ignore[arg-type]
        ),
    )

    with pytest.raises(ManifestValidationError) as raised:
        compile_manifest("acme", built)

    assert raised.value.path == path
