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


@pytest.mark.parametrize(
    ("with_primary", "ids", "match"),
    [
        pytest.param(True, ["primary"], "duplicates channel id", id="enrichment_reuses_primary_id"),
        pytest.param(False, ["same", "same"], "duplicates", id="two_enrichments_share_an_id"),
    ],
)
def test_duplicate_channel_ids_rejected(with_primary: bool, ids: list[str], match: str) -> None:
    bindings = [EnrichmentBinding(source=ControlledSource(), declaration=decl(i)) for i in ids]
    primary = ControlledSource() if with_primary else None
    with pytest.raises(CompositeSourceError, match=match):
        CompositeSource(primary=primary, enrichments=bindings)


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


@pytest.mark.parametrize(
    ("signal", "channel", "fragments"),
    [
        pytest.param(SignalKind.CONTENT, "hook", ["primary ownership"], id="rejected"),
        pytest.param(
            SignalKind.TIMING,
            "otel",
            ["enrichments[0]", "otel", "timing"],
            id="message_names_index_channel_signal",
        ),
    ],
)
def test_enrichment_primary_ownership_rejected(
    signal: SignalKind, channel: str, fragments: list[str]
) -> None:
    cap = ChannelCapability(signal, SignalOwnership.PRIMARY)
    binding = EnrichmentBinding(
        source=ControlledSource(),
        declaration=decl(channel, capabilities=(cap,)),
    )
    with pytest.raises(CompositeSourceError) as exc:
        CompositeSource(enrichments=[binding])
    assert all(fragment in str(exc.value) for fragment in fragments)


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
    with pytest.raises(CompositeSourceError, match="ambiguous fallback") as exc:
        CompositeSource(enrichments=[a, b])
    msg = str(exc.value)
    assert "hook" in msg
    assert "otel" in msg
    assert "content" in msg


@pytest.mark.parametrize(
    "ownerships",
    [
        pytest.param([SignalOwnership.FALLBACK], id="single_fallback"),
        pytest.param(
            [SignalOwnership.ENRICHMENT, SignalOwnership.ENRICHMENT], id="overlapping_enrichment"
        ),
        pytest.param(
            [SignalOwnership.ENRICHMENT, SignalOwnership.FALLBACK], id="enrichment_and_one_fallback"
        ),
    ],
)
def test_valid_signal_ownership_is_accepted(ownerships: list[SignalOwnership]) -> None:
    bindings = [
        EnrichmentBinding(
            source=ControlledSource(),
            declaration=decl(
                name, capabilities=(ChannelCapability(SignalKind.CONTENT, ownership),)
            ),
        )
        for name, ownership in zip(("hook", "otel"), ownerships, strict=False)
    ]
    assert CompositeSource(enrichments=bindings) is not None
