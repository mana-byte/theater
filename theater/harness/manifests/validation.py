"""Structural validation for immutable harness manifests."""

from __future__ import annotations

from collections import defaultdict
from typing import NoReturn

from theater.constants.core import HARNESS_NAME
from theater.constants.harness import (
    HARNESS_APPROVAL_POLICIES,
    HARNESS_CHANNEL_ID_MAX_CHARS,
    HARNESS_MANIFEST_API_VERSION,
)
from theater.formatting import display_width
from theater.harness.contracts.channels import (
    ChannelBounds,
    ChannelCapability,
    ChannelDeclaration,
    ChannelKind,
    SignalKind,
    SignalOwnership,
)
from theater.harness.contracts.manifest import (
    HarnessManifest,
    HookChannelManifest,
    IdentityManifest,
    LaunchManifest,
    LineageManifest,
    ModelDiscoveryManifest,
    ObservationManifest,
    OtelChannelManifest,
    ScreenManifest,
    SourceManifest,
    UnavailableChannelManifest,
)
from theater.trajectory import TrajectoryCapabilities

_DURABLE_KINDS = frozenset({ChannelKind.TRANSCRIPT, ChannelKind.DATABASE})


class ManifestValidationError(ValueError):
    """A path-qualified manifest error that a loader may wrap."""

    def __init__(self, name: str, path: str, message: str) -> None:
        self.name = name
        self.path = path
        self.message = message
        super().__init__(f"manifest {name!r}.{path}: {message}")


def validate_manifest(name: str, manifest: HarnessManifest) -> None:
    """Raise a path-qualified error unless ``manifest`` is safe to compile."""
    _validate_name(name)
    if not isinstance(manifest, HarnessManifest):
        _fail(name, "root", f"expected HarnessManifest, got {type(manifest).__name__}")
    if type(manifest.api_version) is not int:
        _fail(name, "api_version", "must be an integer")
    if manifest.api_version != HARNESS_MANIFEST_API_VERSION:
        _fail(
            name,
            "api_version",
            f"unsupported API version {manifest.api_version!r}; supported version is "
            f"{HARNESS_MANIFEST_API_VERSION}",
        )
    _validate_text(name, "binary", manifest.binary)
    _validate_icon(name, manifest.icon)
    _validate_binaries(name, manifest)
    _validate_aliases(name, manifest)
    _validate_launch(name, manifest.launch)
    _validate_observation(name, manifest.observation)
    _validate_models(name, manifest.models)


def _validate_name(name: object) -> None:
    display = name if isinstance(name, str) else "<invalid>"
    if not isinstance(name, str) or not HARNESS_NAME.fullmatch(name):
        _fail(
            display,
            "name",
            "must use lowercase letters, digits, '_' or '-', starting with a letter or digit",
        )


def _validate_text(name: str, path: str, value: object) -> None:
    if not isinstance(value, str) or not value.strip():
        _fail(name, path, "must be a non-blank string")


def _validate_icon(name: str, icon: object) -> None:
    if not isinstance(icon, str) or not icon or not icon.isprintable():
        _fail(name, "icon", "must contain printable codepoints")
    width = display_width(icon)
    if width != 1:
        _fail(name, "icon", f"must occupy exactly one terminal cell, got {width}")


def _validate_binaries(name: str, manifest: HarnessManifest) -> None:
    if manifest._binaries_are_text:
        _fail(name, "binaries", "must be a frozen set of binary names, not a string")
    if not isinstance(manifest.binaries, frozenset):
        _fail(name, "binaries", "must be a frozen set of binary names")
    for index, binary in enumerate(sorted(manifest.binaries, key=repr)):
        _validate_text(name, f"binaries[{index}]", binary)
    if manifest.binary in manifest.binaries:
        _fail(name, "binaries", "must not repeat the primary binary")


def _validate_aliases(name: str, manifest: HarnessManifest) -> None:
    if manifest._aliases_are_text:
        _fail(name, "aliases", "must be a tuple of aliases, not a string")
    if not isinstance(manifest.aliases, tuple):
        _fail(name, "aliases", "must be a tuple of aliases")
    seen: set[str] = set()
    for index, alias in enumerate(manifest.aliases):
        _validate_text(name, f"aliases[{index}]", alias)
        if alias in seen:
            _fail(name, f"aliases[{index}]", f"duplicates alias {alias!r}")
        if alias == name:
            _fail(name, f"aliases[{index}]", "must not repeat the canonical harness name")
        seen.add(alias)


def _validate_launch(name: str, launch: object) -> None:
    if not isinstance(launch, LaunchManifest):
        _fail(name, "launch", f"expected LaunchManifest, got {type(launch).__name__}")
    if not callable(launch.planner):
        _fail(name, "launch.planner", "must be callable")
    if not isinstance(launch.approvals, tuple):
        _fail(name, "launch.approvals", "must be an ordered sequence of approval policies")
    if not launch.approvals:
        _fail(name, "launch.approvals", "must declare at least one supported approval policy")
    for approval in launch.approvals:
        if not isinstance(approval, str) or approval not in HARNESS_APPROVAL_POLICIES:
            _fail(
                name,
                "launch.approvals",
                f"contains {approval!r}; expected only {', '.join(HARNESS_APPROVAL_POLICIES)}",
            )
    if len(set(launch.approvals)) != len(launch.approvals):
        _fail(name, "launch.approvals", "must not repeat an approval policy")
    _validate_launch_options(name, launch)


def _validate_launch_options(name: str, launch: LaunchManifest) -> None:
    for field in ("supports_model", "supports_reasoning_effort", "supports_resume"):
        if type(getattr(launch, field)) is not bool:
            _fail(name, f"launch.{field}", "must be a boolean")
    if launch.resume_planner is not None and not callable(launch.resume_planner):
        _fail(name, "launch.resume_planner", "must be callable or null")
    if type(launch.resume_takes_prompt) is not bool:
        _fail(name, "launch.resume_takes_prompt", "must be a boolean")
    if not isinstance(launch.resume_strategy, str) or launch.resume_strategy not in {
        "continue",
        "fork",
    }:
        _fail(name, "launch.resume_strategy", "must be 'continue' or 'fork'")
    _validate_resume_options(name, launch)


def _validate_resume_options(name: str, launch: LaunchManifest) -> None:
    if launch.supports_resume:
        return
    if launch.resume_planner is not None:
        _fail(name, "launch.resume_planner", "requires supports_resume=True")
    if not launch.resume_takes_prompt:
        _fail(name, "launch.resume_takes_prompt", "requires supports_resume=True")
    if launch.resume_strategy != "continue":
        _fail(name, "launch.resume_strategy", "requires supports_resume=True")


def _validate_observation(name: str, observation: object) -> None:
    if not isinstance(observation, ObservationManifest):
        _fail(
            name, "observation", f"expected ObservationManifest, got {type(observation).__name__}"
        )
    _validate_screen(name, observation.screen)
    if not isinstance(observation.enrichments, tuple):
        _fail(name, "observation.enrichments", "must be a tuple of channel declarations")
    if not isinstance(observation.identity, IdentityManifest):
        _fail(
            name,
            "observation.identity",
            f"expected IdentityManifest, got {type(observation.identity).__name__}",
        )
    if not isinstance(observation.lineage, LineageManifest):
        _fail(
            name,
            "observation.lineage",
            f"expected LineageManifest, got {type(observation.lineage).__name__}",
        )
    if not isinstance(observation.trajectory_capabilities, TrajectoryCapabilities):
        _fail(
            name,
            "observation.trajectory_capabilities",
            f"expected TrajectoryCapabilities, got "
            f"{type(observation.trajectory_capabilities).__name__}",
        )

    channels: list[tuple[ChannelDeclaration, str]] = []
    if observation.primary is not None:
        _validate_source(name, observation.primary)
        primary = observation.primary.channel
        if primary.kind not in _DURABLE_KINDS:
            _fail(
                name,
                "observation.primary.channel.kind",
                "must be transcript or database for a primary source",
            )
        channels.append((primary, "observation.primary.channel"))

    _validate_identity(name, observation)
    _validate_lineage(name, observation)

    for index, enrichment in enumerate(observation.enrichments):
        path = f"observation.enrichments[{index}]"
        declaration = _validate_enrichment(name, path, enrichment)
        if declaration.kind in _DURABLE_KINDS:
            _fail(name, f"{path}.kind", "durable channels must be declared as observation.primary")
        channels.append((declaration, path))

    _validate_channel_ids(name, channels)
    _validate_ownership(name, channels)


def _validate_screen(name: str, screen: object) -> None:
    if not isinstance(screen, ScreenManifest):
        _fail(name, "observation.screen", f"expected ScreenManifest, got {type(screen).__name__}")
    if not callable(screen.classifier):
        _fail(name, "observation.screen.classifier", "must be callable")


def _validate_source(name: str, source: object) -> None:
    if not isinstance(source, SourceManifest):
        _fail(
            name,
            "observation.primary",
            f"expected SourceManifest or null, got {type(source).__name__}",
        )
    if not callable(source.factory):
        _fail(name, "observation.primary.factory", "must be callable")
    _validate_channel(name, "observation.primary.channel", source.channel)


def _validate_enrichment(name: str, path: str, enrichment: object) -> ChannelDeclaration:
    if isinstance(enrichment, ChannelDeclaration):
        _validate_channel(name, path, enrichment)
        return enrichment
    if isinstance(enrichment, HookChannelManifest):
        _validate_channel(name, f"{path}.declaration", enrichment.declaration)
        if enrichment.declaration.kind is not ChannelKind.HOOK:
            _fail(name, f"{path}.declaration.kind", "must be hook")
        _validate_optional_reason(name, f"{path}.unavailable_reason", enrichment.unavailable_reason)
        return enrichment.declaration
    if isinstance(enrichment, OtelChannelManifest):
        _validate_channel(name, f"{path}.declaration", enrichment.declaration)
        if enrichment.declaration.kind is not ChannelKind.OTEL:
            _fail(name, f"{path}.declaration.kind", "must be otel")
        _validate_optional_reason(name, f"{path}.unavailable_reason", enrichment.unavailable_reason)
        return enrichment.declaration
    if isinstance(enrichment, UnavailableChannelManifest):
        _validate_channel(name, f"{path}.declaration", enrichment.declaration)
        _validate_text(name, f"{path}.reason", enrichment.reason)
        return enrichment.declaration
    return _fail(name, path, f"expected a channel manifest, got {type(enrichment).__name__}")


def _validate_optional_reason(name: str, path: str, reason: object) -> None:
    if reason is not None:
        _validate_text(name, path, reason)


def _validate_channel(name: str, path: str, channel: object) -> None:
    if not isinstance(channel, ChannelDeclaration):
        _fail(name, path, f"expected ChannelDeclaration, got {type(channel).__name__}")
    if (
        not isinstance(channel.id, str)
        or len(channel.id) > HARNESS_CHANNEL_ID_MAX_CHARS
        or not HARNESS_NAME.fullmatch(channel.id)
    ):
        _fail(
            name,
            f"{path}.id",
            "must be a bounded canonical identifier using lowercase letters, digits, '_' or '-'",
        )
    if not isinstance(channel.kind, ChannelKind):
        _fail(name, f"{path}.kind", "must be a ChannelKind")
    if not isinstance(channel.capabilities, tuple):
        _fail(name, f"{path}.capabilities", "must be a tuple of ChannelCapability values")
    seen: set[SignalKind] = set()
    for index, capability in enumerate(channel.capabilities):
        capability_path = f"{path}.capabilities[{index}]"
        if not isinstance(capability, ChannelCapability):
            _fail(
                name,
                capability_path,
                f"expected ChannelCapability, got {type(capability).__name__}",
            )
        if not isinstance(capability.signal, SignalKind):
            _fail(name, f"{capability_path}.signal", "must be a SignalKind")
        if not isinstance(capability.ownership, SignalOwnership):
            _fail(name, f"{capability_path}.ownership", "must be a SignalOwnership")
        if capability.signal in seen:
            _fail(name, f"{capability_path}.signal", "must not repeat a signal in one channel")
        seen.add(capability.signal)
    _validate_bounds(name, f"{path}.bounds", channel.bounds)


def _validate_bounds(name: str, path: str, bounds: object) -> None:
    if not isinstance(bounds, ChannelBounds):
        _fail(name, path, f"expected ChannelBounds, got {type(bounds).__name__}")
    for field in ("max_queue", "max_payload_bytes"):
        value = getattr(bounds, field)
        if type(value) is not int or value <= 0:
            _fail(name, f"{path}.{field}", "must be a positive integer")


def _validate_identity(name: str, observation: ObservationManifest) -> None:
    identity = observation.identity
    has_source = observation.primary is not None
    for field, label in (
        ("stream_floor", "observation.identity.stream_floor"),
        ("transcript_candidates", "observation.identity.transcript_candidates"),
        ("receipt_validator", "observation.identity.receipt_validator"),
        ("operator_candidate_admitter", "observation.identity.operator_candidate_admitter"),
    ):
        callback = getattr(identity, field)
        if callback is None:
            continue
        if not callable(callback):
            _fail(name, label, "must be callable or null")
        if not has_source:
            _fail(name, label, "requires a primary source")


def _validate_lineage(name: str, observation: ObservationManifest) -> None:
    callback = observation.lineage.native_children
    if callback is None:
        return
    if not callable(callback):
        _fail(name, "observation.lineage.native_children", "must be callable or null")
    if observation.primary is None:
        _fail(name, "observation.lineage.native_children", "requires a primary source")


def _validate_channel_ids(name: str, channels: list[tuple[ChannelDeclaration, str]]) -> None:
    claimed: dict[str, str] = {}
    for channel, path in channels:
        previous = claimed.get(channel.id)
        if previous is not None:
            _fail(
                name, f"{path}.id", f"duplicates channel id {channel.id!r} declared at {previous}"
            )
        claimed[channel.id] = path


def _validate_ownership(name: str, channels: list[tuple[ChannelDeclaration, str]]) -> None:
    claimed: defaultdict[SignalKind, list[tuple[str, int, ChannelCapability]]] = defaultdict(list)
    for channel, path in channels:
        for index, capability in enumerate(channel.capabilities):
            claimed[capability.signal].append((path, index, capability))
    for signal, entries in claimed.items():
        owners = [entry for entry in entries if entry[2].ownership is not SignalOwnership.FALLBACK]
        fallbacks = [entry for entry in entries if entry[2].ownership is SignalOwnership.FALLBACK]
        if len(owners) > 1:
            paths = ", ".join(f"{path}.capabilities[{index}]" for path, index, _ in owners)
            _fail(
                name,
                f"{owners[1][0]}.capabilities[{owners[1][1]}].ownership",
                f"signal {signal.value!r} overlaps {paths}; mark one owner as fallback",
            )
        if fallbacks and not owners:
            path, index, _ = fallbacks[0]
            _fail(
                name,
                f"{path}.capabilities[{index}].ownership",
                f"fallback for signal {signal.value!r} needs a primary or enrichment owner",
            )


def _validate_models(name: str, models: object) -> None:
    if models is None:
        return
    if not isinstance(models, ModelDiscoveryManifest):
        _fail(
            name, "models", f"expected ModelDiscoveryManifest or null, got {type(models).__name__}"
        )
    if not callable(models.discoverer):
        _fail(name, "models.discoverer", "must be callable")


def _fail(name: str, path: str, message: str) -> NoReturn:
    raise ManifestValidationError(name, path, message)


__all__ = ["ManifestValidationError", "validate_manifest"]
