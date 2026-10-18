"""Hook and OTLP channel manifest declarations, projections and shipped unavailability."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

import pytest

from tests.support.hook_channel_rig import hook_manifest
from tests.support.otel_channel_rig import hook_channel, otel_manifest
from theater.harness.builtin.plugins.claude.manifest import MANIFEST as CLAUDE_MANIFEST
from theater.harness.builtin.plugins.codex.manifest import MANIFEST as CODEX_MANIFEST
from theater.harness.builtin.plugins.opencode.manifest import MANIFEST as OPENCODE_MANIFEST
from theater.harness.builtin.plugins.vibe.manifest import MANIFEST as VIBE_MANIFEST
from theater.harness.contracts.callbacks import HookCorrelationContext
from theater.harness.contracts.channels import ChannelKind, SignalKind
from theater.harness.contracts.manifest import (
    HarnessManifest,
    HookChannelManifest,
    ObservationManifest,
    OtelChannelManifest,
)
from theater.harness.loading.models import LoadedPlugin
from theater.harness.manifests.compiler import compile_manifest
from theater.harness.manifests.validation import ManifestValidationError
from theater.harness.registry.diagnostics import project_plugin


@pytest.mark.parametrize(
    ("name", "built"),
    (
        ("codex", CODEX_MANIFEST),
        ("opencode", OPENCODE_MANIFEST),
        ("vibe", VIBE_MANIFEST),
    ),
)
def test_shipped_manifests_declare_native_hooks_unavailable(name, built) -> None:
    compiled = compile_manifest(name, built)
    channels = tuple(
        channel
        for channel in compiled.observer.enrichment_manifests()
        if isinstance(channel, HookChannelManifest)
    )

    assert len(channels) == 1
    channel = channels[0]
    assert isinstance(channel, HookChannelManifest)
    assert channel.declaration.id == "native-hooks"
    assert channel.declaration.kind is ChannelKind.HOOK
    assert channel.declaration.capabilities == ()
    assert channel.bindings == ()
    assert channel.installer is None
    assert channel.unavailable_reason


def test_hook_manifest_rejects_unavailable_active_and_duplicate_events() -> None:
    built = hook_manifest()
    channel = built.observation.hook_channels[0]
    invalid = HookChannelManifest(
        declaration=channel.declaration,
        bindings=channel.bindings,
        installer=channel.installer,
        unavailable_reason="not installed",
    )
    with pytest.raises(ManifestValidationError) as raised:
        compile_manifest(
            "acme",
            HarnessManifest(
                api_version=built.api_version,
                binary=built.binary,
                icon=built.icon,
                launch=built.launch,
                observation=ObservationManifest(
                    primary=built.observation.primary,
                    screen=built.observation.screen,
                    enrichments=(invalid,),
                ),
            ),
        )
    assert raised.value.path == "observation.enrichments[0].bindings"

    empty = HookChannelManifest(declaration=channel.declaration)
    with pytest.raises(ManifestValidationError) as raised:
        compile_manifest(
            "acme",
            HarnessManifest(
                api_version=built.api_version,
                binary=built.binary,
                icon=built.icon,
                launch=built.launch,
                observation=ObservationManifest(
                    primary=built.observation.primary,
                    screen=built.observation.screen,
                    enrichments=(empty,),
                ),
            ),
        )
    assert raised.value.path == "observation.enrichments[0].bindings"


def test_hook_contexts_freeze_only_json_values() -> None:
    payload = {"nested": ({"items": ["value"]},)}
    context = HookCorrelationContext(
        participant_id="participant",
        channel_id="native-hooks",
        event="tool.finished",
        payload=payload,
    )
    payload["nested"] = ()
    nested = context.payload["nested"]
    assert isinstance(nested, tuple)
    assert isinstance(nested[0], Mapping)
    assert nested[0]["items"] == ("value",)
    with pytest.raises(TypeError):
        nested[0]["items"] = ()
    with pytest.raises(TypeError, match="JSON-compatible"):
        HookCorrelationContext(
            participant_id="participant",
            channel_id="native-hooks",
            event="tool.finished",
            payload={"unsupported": {"value"}},
        )


def test_manifest_projection_includes_hook_and_otel_native_bindings() -> None:
    manifest = otel_manifest()
    hook = hook_channel()
    manifest = replace(
        manifest,
        observation=replace(
            manifest.observation,
            enrichments=(hook, *manifest.observation.enrichments),
        ),
    )
    compiled = compile_manifest("acme", manifest)
    row = project_plugin(
        {
            "name": "acme",
            "icon": "A",
            "binary": "acme",
            "installed": True,
            "path": "/usr/bin/acme",
            "source": "local",
            "error": None,
        },
        LoadedPlugin(
            path=Path("/tmp/acme"),
            source="local",
            name="acme",
            harness=compiled,
            manifest=manifest,
        ),
        None,
    )

    channels = {channel["id"]: channel for channel in row["channels"]}
    assert channels["native-hook"]["bindings"] == [
        {"event": "tool.finished", "delivery": "best_effort", "signals": ["lifecycle"]}
    ]
    assert channels["native-otel"]["protocol"] == "otlp_http_json"
    assert channels["native-otel"]["bindings"] == [
        {"name": "tool.finished", "signal": "logs", "signals": ["tool"]}
    ]


def test_native_otel_manifest_validation_and_shipped_unavailability() -> None:
    manifest = otel_manifest()
    channel = manifest.observation.otel_channels[0]
    invalid = replace(channel, declaration=replace(channel.declaration, kind=ChannelKind.HOOK))
    with pytest.raises(ManifestValidationError) as raised:
        compile_manifest(
            "acme",
            replace(
                manifest,
                observation=replace(manifest.observation, enrichments=(invalid,)),
            ),
        )
    assert raised.value.path == "observation.enrichments[0].declaration.kind"
    unavailable = replace(channel, unavailable_reason="not safe")
    with pytest.raises(ManifestValidationError) as raised:
        compile_manifest(
            "acme",
            replace(
                manifest,
                observation=replace(manifest.observation, enrichments=(unavailable,)),
            ),
        )
    assert raised.value.path == "observation.enrichments[0].bindings"
    binding = channel.bindings[0]
    invalid_cases = (
        (
            replace(channel, bindings=(binding, binding)),
            "observation.enrichments[0].bindings[1].name",
        ),
        (
            replace(channel, bindings=(replace(binding, signals=(SignalKind.USAGE,)),)),
            "observation.enrichments[0].bindings[0].signals[0]",
        ),
        (
            replace(channel, bounds=replace(channel.bounds, max_records=0)),
            "observation.enrichments[0].bounds.max_records",
        ),
        (
            replace(channel, installer=None),
            "observation.enrichments[0].installer",
        ),
        (
            replace(
                channel,
                correlation=replace(channel.correlation, auth_header="not a header"),
            ),
            "observation.enrichments[0].correlation.auth_header",
        ),
        (
            replace(
                channel,
                correlation=replace(channel.correlation, auth_header="content-type"),
            ),
            "observation.enrichments[0].correlation.auth_header",
        ),
        (
            replace(channel, bindings=(replace(binding, decoder=object()),)),
            "observation.enrichments[0].bindings[0].decoder",
        ),
    )
    for invalid_channel, path in invalid_cases:
        with pytest.raises(ManifestValidationError) as raised:
            compile_manifest(
                "acme",
                replace(
                    manifest,
                    observation=replace(manifest.observation, enrichments=(invalid_channel,)),
                ),
            )
        assert raised.value.path == path

    expected = {
        "claude": "emitted schema/fan-out/launch correlation unverified.",
        "codex": "endpoint choice replaces exporter; no safe fan-out.",
        "opencode": "one endpoint, no safe fan-out or stable join.",
        "vibe": "installs one global provider/exporter; exporter theft.",
    }
    for name, built in (
        ("claude", CLAUDE_MANIFEST),
        ("codex", CODEX_MANIFEST),
        ("opencode", OPENCODE_MANIFEST),
        ("vibe", VIBE_MANIFEST),
    ):
        compiled = compile_manifest(name, built)
        channels = compiled.observer.enrichment_manifests()
        native = next(channel for channel in channels if isinstance(channel, OtelChannelManifest))
        assert native.declaration.id == "native-otel"
        assert native.declaration.kind is ChannelKind.OTEL
        assert native.declaration.capabilities == ()
        assert native.bindings == () and native.installer is None
        assert native.unavailable_reason == expected[name]
