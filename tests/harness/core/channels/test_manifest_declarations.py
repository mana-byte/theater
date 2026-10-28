"""Hook and OTLP channel manifest declarations and projections."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

import pytest

from tests.support.hook_channel_rig import hook_manifest
from tests.support.otel_channel_rig import hook_channel, otel_manifest
from theater.harness.contracts.callbacks import HookCorrelationContext
from theater.harness.contracts.channels import ChannelKind, SignalKind
from theater.harness.contracts.manifest import (
    HarnessManifest,
    HookChannelManifest,
    ObservationManifest,
)
from theater.harness.loading.models import LoadedPlugin
from theater.harness.manifests.compiler import compile_manifest
from theater.harness.manifests.validation import ManifestValidationError
from theater.harness.registry.diagnostics import project_plugin


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


def test_native_otel_manifest_validation() -> None:
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
