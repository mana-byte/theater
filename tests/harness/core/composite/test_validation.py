"""Composite source construction rejects malformed primaries, enrichments and fallbacks."""

from __future__ import annotations

import pytest

from tests.support.composite_sources import ControlledSource, decl
from theater.constants.harness import HARNESS_CHANNEL_ID_MAX_CHARS
from theater.harness.channels import CompositeSource, EnrichmentBinding
from theater.harness.channels.composite import CompositeSourceError
from theater.harness.contracts.channels import (
    ChannelCapability,
    ChannelDeclaration,
    ChannelKind,
    SignalKind,
    SignalOwnership,
)


def test_primary_must_be_source_or_none() -> None:
    with pytest.raises(CompositeSourceError, match="primary must implement Source"):
        CompositeSource(primary="not-a-source")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "channel_id", ["", "Primary", "bad id", "a" * (HARNESS_CHANNEL_ID_MAX_CHARS + 1), None]
)
def test_primary_channel_id_must_be_bounded_and_canonical(channel_id: object) -> None:
    with pytest.raises(CompositeSourceError, match="primary_channel_id"):
        CompositeSource(
            primary=ControlledSource(),
            primary_channel_id=channel_id,  # type: ignore[arg-type]
        )


def test_enrichment_must_be_binding() -> None:
    with pytest.raises(CompositeSourceError, match="enrichments"):
        CompositeSource(enrichments=["not-a-binding"])  # type: ignore[arg-type]


def test_enrichment_source_must_implement_source() -> None:
    with pytest.raises(CompositeSourceError, match="must implement Source"):
        CompositeSource(
            enrichments=[EnrichmentBinding(source="x", declaration=decl())]  # type: ignore[arg-type]
        )


def test_duplicate_channel_ids_rejected() -> None:
    primary = ControlledSource()
    binding = EnrichmentBinding(source=ControlledSource(), declaration=decl("primary"))
    with pytest.raises(CompositeSourceError, match="duplicates channel id"):
        CompositeSource(primary=primary, enrichments=[binding])


def test_duplicate_enrichment_ids_rejected() -> None:
    b1 = EnrichmentBinding(source=ControlledSource(), declaration=decl("same"))
    b2 = EnrichmentBinding(source=ControlledSource(), declaration=decl("same"))
    with pytest.raises(CompositeSourceError, match="duplicates"):
        CompositeSource(enrichments=[b1, b2])


def test_durable_enrichment_rejected() -> None:
    binding = EnrichmentBinding(
        source=ControlledSource(),
        declaration=ChannelDeclaration(id="transcript", kind=ChannelKind.TRANSCRIPT),
    )
    with pytest.raises(CompositeSourceError, match="must not be a durable channel"):
        CompositeSource(enrichments=[binding])


def test_no_primary_no_enrichments_valid() -> None:
    composite = CompositeSource()
    assert composite is not None


@pytest.mark.parametrize("bad_timeout", [0, -1, float("nan"), float("inf"), "x", None])
def test_invalid_enrichment_timeout_rejected(bad_timeout: object) -> None:
    with pytest.raises(CompositeSourceError, match="enrichment_timeout"):
        CompositeSource(enrichment_timeout=bad_timeout)  # type: ignore[arg-type]


def test_enrichment_primary_ownership_rejected() -> None:
    cap = ChannelCapability(SignalKind.CONTENT, SignalOwnership.PRIMARY)
    binding = EnrichmentBinding(
        source=ControlledSource(),
        declaration=decl("hook", capabilities=(cap,)),
    )
    with pytest.raises(CompositeSourceError, match="primary ownership"):
        CompositeSource(enrichments=[binding])


def test_enrichment_primary_ownership_message_names_index_channel_signal() -> None:
    cap = ChannelCapability(SignalKind.TIMING, SignalOwnership.PRIMARY)
    binding = EnrichmentBinding(
        source=ControlledSource(),
        declaration=decl("otel", capabilities=(cap,)),
    )
    with pytest.raises(CompositeSourceError) as exc:
        CompositeSource(enrichments=[binding])
    msg = str(exc.value)
    assert "enrichments[0]" in msg
    assert "otel" in msg
    assert "timing" in msg


def test_duplicate_fallback_for_same_signal_rejected() -> None:
    cap_a = ChannelCapability(SignalKind.CONTENT, SignalOwnership.FALLBACK)
    cap_b = ChannelCapability(SignalKind.CONTENT, SignalOwnership.FALLBACK)
    a = EnrichmentBinding(
        source=ControlledSource(),
        declaration=decl("hook", capabilities=(cap_a,)),
    )
    b = EnrichmentBinding(
        source=ControlledSource(),
        declaration=decl("otel", capabilities=(cap_b,)),
    )
    with pytest.raises(CompositeSourceError, match="ambiguous fallback"):
        CompositeSource(enrichments=[a, b])


def test_duplicate_fallback_message_names_both_channels_and_signal() -> None:
    cap_a = ChannelCapability(SignalKind.CONTENT, SignalOwnership.FALLBACK)
    cap_b = ChannelCapability(SignalKind.CONTENT, SignalOwnership.FALLBACK)
    a = EnrichmentBinding(
        source=ControlledSource(),
        declaration=decl("hook", capabilities=(cap_a,)),
    )
    b = EnrichmentBinding(
        source=ControlledSource(),
        declaration=decl("otel", capabilities=(cap_b,)),
    )
    with pytest.raises(CompositeSourceError) as exc:
        CompositeSource(enrichments=[a, b])
    msg = str(exc.value)
    assert "hook" in msg
    assert "otel" in msg
    assert "content" in msg


def test_single_fallback_accepted() -> None:
    cap = ChannelCapability(SignalKind.CONTENT, SignalOwnership.FALLBACK)
    binding = EnrichmentBinding(
        source=ControlledSource(),
        declaration=decl("hook", capabilities=(cap,)),
    )
    composite = CompositeSource(enrichments=[binding])
    assert composite is not None


def test_overlapping_enrichment_ownership_accepted() -> None:
    cap_a = ChannelCapability(SignalKind.CONTENT, SignalOwnership.ENRICHMENT)
    cap_b = ChannelCapability(SignalKind.CONTENT, SignalOwnership.ENRICHMENT)
    a = EnrichmentBinding(
        source=ControlledSource(),
        declaration=decl("hook", capabilities=(cap_a,)),
    )
    b = EnrichmentBinding(
        source=ControlledSource(),
        declaration=decl("otel", capabilities=(cap_b,)),
    )
    composite = CompositeSource(enrichments=[a, b])
    assert composite is not None


def test_mixed_enrichment_and_single_fallback_accepted() -> None:
    cap_a = ChannelCapability(SignalKind.CONTENT, SignalOwnership.ENRICHMENT)
    cap_b = ChannelCapability(SignalKind.CONTENT, SignalOwnership.FALLBACK)
    a = EnrichmentBinding(
        source=ControlledSource(),
        declaration=decl("hook", capabilities=(cap_a,)),
    )
    b = EnrichmentBinding(
        source=ControlledSource(),
        declaration=decl("otel", capabilities=(cap_b,)),
    )
    composite = CompositeSource(enrichments=[a, b])
    assert composite is not None
