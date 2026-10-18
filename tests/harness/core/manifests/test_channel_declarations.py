"""Manifest channel, identity and lineage declarations are validated and compiled."""

from __future__ import annotations

import pytest

from tests.support.manifest_rig import manifest, primary_channel, screen, source
from theater.constants.harness import (
    HARNESS_CHANNEL_HEALTH_DIAGNOSTIC_MAX_CHARS,
    HARNESS_CHANNEL_HEALTH_MAX_DIAGNOSTICS,
    HARNESS_CHANNEL_ID_MAX_CHARS,
)
from theater.harness.contracts.callbacks import ScreenContext
from theater.harness.contracts.channels import (
    ChannelBounds,
    ChannelCapability,
    ChannelDeclaration,
    ChannelHealth,
    ChannelKind,
    SignalKind,
    SignalOwnership,
)
from theater.harness.contracts.harness import Harness
from theater.harness.contracts.manifest import (
    IdentityManifest,
    LineageManifest,
    ObservationManifest,
    ScreenManifest,
    SourceManifest,
)
from theater.harness.contracts.observation import ScreenKind, ScreenReading
from theater.harness.manifests.compiler import compile_manifest
from theater.harness.manifests.strategies import ScreenMarker, screen_classifier_from_markers
from theater.harness.manifests.validation import ManifestValidationError


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
