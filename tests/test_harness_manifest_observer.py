"""Contract tests for the compiled ManifestHarnessObserver."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest

from theater.harness.contracts.callbacks import (
    NativeChildrenContext,
    OperatorCandidateContext,
    ReceiptValidationContext,
    StreamFloorContext,
    TranscriptCandidatesContext,
)
from theater.harness.contracts.launch import NativeChild
from theater.harness.contracts.manifest import (
    MANIFEST_API_VERSION,
    HarnessManifest,
    IdentityManifest,
    LaunchManifest,
    LineageManifest,
    ObservationManifest,
    ScreenManifest,
    SourceManifest,
)
from theater.harness.contracts.observation import HarnessObserver, ScreenConfidence, ScreenKind
from theater.harness.contracts.source import Batch, Source, StreamPoint, TranscriptCandidate
from theater.harness.manifests.compiler import ManifestHarnessObserver, compile_manifest
from theater.trajectory import TrajectoryCapabilities


class StubSource(Source):
    async def read(self) -> Batch:
        return Batch()


def _plan(context):
    from theater.harness.contracts.launch import LaunchPlan

    return LaunchPlan(argv=["acme", context.participant_id])


def _source(_context) -> Source:
    return StubSource()


def _screen(_context):
    from theater.harness.contracts.observation import ScreenReading

    return ScreenReading(ScreenKind.PROMPT, ScreenConfidence.HIGH)


def _primary_channel():
    from theater.harness.contracts.channels import ChannelDeclaration, ChannelKind

    return ChannelDeclaration(id="primary", kind=ChannelKind.TRANSCRIPT)


def _observer(
    *,
    identity: IdentityManifest | None = None,
    lineage: LineageManifest | None = None,
    trajectory_capabilities: TrajectoryCapabilities | None = None,
    primary: SourceManifest | None | None = ...,
) -> ManifestHarnessObserver:
    kwargs: dict = {}
    if identity is not None:
        kwargs["identity"] = identity
    if lineage is not None:
        kwargs["lineage"] = lineage
    if trajectory_capabilities is not None:
        kwargs["trajectory_capabilities"] = trajectory_capabilities
    if primary is not ...:
        kwargs["primary"] = primary
    observation = ObservationManifest(
        screen=ScreenManifest(classifier=_screen),
        **kwargs,
    )
    return ManifestHarnessObserver(observation)


# --- native_children --------------------------------------------------------


def test_native_children_forwards_exact_context_and_value() -> None:
    seen: list[NativeChildrenContext] = []
    expected = [NativeChild(session_id="child-1")]
    callback = lambda ctx: seen.append(ctx) or expected  # noqa: E731
    observer = _observer(
        primary=SourceManifest(factory=_source, channel=_primary_channel()),
        lineage=LineageManifest(native_children=callback),
    )
    result = observer.native_children(Path("/work/transcript.jsonl"))
    assert result == expected
    assert seen == [NativeChildrenContext(transcript=Path("/work/transcript.jsonl"))]


def test_native_children_absent_returns_superclass_default() -> None:
    observer = _observer(
        primary=SourceManifest(factory=_source, channel=_primary_channel()),
    )
    assert observer.native_children(Path("/work/t.jsonl")) == []


def test_native_children_wrong_result_type_raises_type_error() -> None:
    callback = lambda _ctx: [object()]  # noqa: E731
    observer = _observer(
        primary=SourceManifest(factory=_source, channel=_primary_channel()),
        lineage=LineageManifest(native_children=callback),
    )
    with pytest.raises(TypeError, match="native children"):
        observer.native_children(Path("/work/t.jsonl"))


def test_native_children_exception_propagates() -> None:
    error = RuntimeError("boom")
    callback = lambda _ctx: (_ for _ in ()).throw(error)  # noqa: E731
    observer = _observer(
        primary=SourceManifest(factory=_source, channel=_primary_channel()),
        lineage=LineageManifest(native_children=callback),
    )
    with pytest.raises(RuntimeError) as raised:
        observer.native_children(Path("/work/t.jsonl"))
    assert raised.value is error


# --- stream_floor -----------------------------------------------------------


def test_stream_floor_forwards_exact_context_and_value() -> None:
    seen: list[StreamFloorContext] = []
    expected = StreamPoint(records=10, size=500)
    callback = lambda ctx: (seen.append(ctx), expected)[1]  # noqa: E731
    observer = _observer(
        primary=SourceManifest(factory=_source, channel=_primary_channel()),
        identity=IdentityManifest(stream_floor=callback),
    )
    result = observer.stream_floor("/work/t.jsonl")
    assert result == expected
    assert seen == [StreamFloorContext(location="/work/t.jsonl")]


def test_stream_floor_absent_returns_superclass_default() -> None:
    observer = _observer(
        primary=SourceManifest(factory=_source, channel=_primary_channel()),
    )
    assert observer.stream_floor("/work/t.jsonl") is None


def test_stream_floor_none_return_is_accepted() -> None:
    callback = lambda _ctx: None  # noqa: E731
    observer = _observer(
        primary=SourceManifest(factory=_source, channel=_primary_channel()),
        identity=IdentityManifest(stream_floor=callback),
    )
    assert observer.stream_floor("/work/t.jsonl") is None


def test_stream_floor_wrong_result_type_raises_type_error() -> None:
    callback = lambda _ctx: "not-a-stream-point"  # noqa: E731
    observer = _observer(
        primary=SourceManifest(factory=_source, channel=_primary_channel()),
        identity=IdentityManifest(stream_floor=callback),
    )
    with pytest.raises(TypeError, match="stream-floor"):
        observer.stream_floor("/work/t.jsonl")


def test_stream_floor_exception_propagates() -> None:
    error = RuntimeError("floor-boom")
    callback = lambda _ctx: (_ for _ in ()).throw(error)  # noqa: E731
    observer = _observer(
        primary=SourceManifest(factory=_source, channel=_primary_channel()),
        identity=IdentityManifest(stream_floor=callback),
    )
    with pytest.raises(RuntimeError) as raised:
        observer.stream_floor("/work/t.jsonl")
    assert raised.value is error


# --- transcript_candidates -------------------------------------------------


def test_transcript_candidates_forwards_exact_context_and_value() -> None:
    seen: list[TranscriptCandidatesContext] = []
    expected = [TranscriptCandidate(location="/work/t.jsonl", session_id="s1")]
    callback = lambda ctx: (seen.append(ctx), expected)[1]  # noqa: E731
    observer = _observer(
        primary=SourceManifest(factory=_source, channel=_primary_channel()),
        identity=IdentityManifest(transcript_candidates=callback),
    )
    result = observer.transcript_candidates(cwd="/work", domain="d", after=1.0)
    assert result == expected
    assert seen == [TranscriptCandidatesContext(cwd="/work", domain="d", after=1.0)]


def test_transcript_candidates_absent_returns_superclass_default() -> None:
    observer = _observer(
        primary=SourceManifest(factory=_source, channel=_primary_channel()),
    )
    assert observer.transcript_candidates(cwd="/work") == []


def test_transcript_candidates_wrong_result_type_raises_type_error() -> None:
    callback = lambda _ctx: [object()]  # noqa: E731
    observer = _observer(
        primary=SourceManifest(factory=_source, channel=_primary_channel()),
        identity=IdentityManifest(transcript_candidates=callback),
    )
    with pytest.raises(TypeError, match="transcript candidates"):
        observer.transcript_candidates(cwd="/work")


def test_transcript_candidates_exception_propagates() -> None:
    error = RuntimeError("cand-boom")
    callback = lambda _ctx: (_ for _ in ()).throw(error)  # noqa: E731
    observer = _observer(
        primary=SourceManifest(factory=_source, channel=_primary_channel()),
        identity=IdentityManifest(transcript_candidates=callback),
    )
    with pytest.raises(RuntimeError) as raised:
        observer.transcript_candidates(cwd="/work")
    assert raised.value is error


# --- validate_transcript_receipt --------------------------------------------


def test_receipt_validator_forwards_exact_context_and_value() -> None:
    seen: list[ReceiptValidationContext] = []
    expected = TranscriptCandidate(location="/work/t.jsonl", session_id="s1")
    callback = lambda ctx: (seen.append(ctx), expected)[1]  # noqa: E731
    observer = _observer(
        primary=SourceManifest(factory=_source, channel=_primary_channel()),
        identity=IdentityManifest(receipt_validator=callback),
    )
    payload: Mapping[str, object] = {"key": "value"}
    result = observer.validate_transcript_receipt(
        payload=payload, cwd="/work", expected_session_id="s1"
    )
    assert result == expected
    assert len(seen) == 1
    assert seen[0].cwd == "/work"
    assert seen[0].expected_session_id == "s1"
    assert seen[0].payload["key"] == "value"


def test_receipt_validation_context_payload_is_immutable_copy() -> None:
    original: dict[str, object] = {"key": "value"}
    ctx = ReceiptValidationContext(payload=original, cwd=None, expected_session_id=None)
    original["key"] = "changed"
    assert ctx.payload["key"] == "value"
    with pytest.raises(TypeError):
        ctx.payload["key"] = "mutate"  # type: ignore[index]


def test_receipt_validator_absent_returns_superclass_refusal() -> None:
    observer = _observer(
        primary=SourceManifest(factory=_source, channel=_primary_channel()),
    )
    with pytest.raises(ValueError, match="does not implement"):
        observer.validate_transcript_receipt(payload={}, cwd="/work", expected_session_id=None)


def test_receipt_validator_wrong_result_type_raises_type_error() -> None:
    callback = lambda _ctx: "not-a-candidate"  # noqa: E731
    observer = _observer(
        primary=SourceManifest(factory=_source, channel=_primary_channel()),
        identity=IdentityManifest(receipt_validator=callback),
    )
    with pytest.raises(TypeError, match="receipt validator"):
        observer.validate_transcript_receipt(payload={}, cwd="/work", expected_session_id=None)


def test_receipt_validator_exception_propagates() -> None:
    error = RuntimeError("receipt-boom")
    callback = lambda _ctx: (_ for _ in ()).throw(error)  # noqa: E731
    observer = _observer(
        primary=SourceManifest(factory=_source, channel=_primary_channel()),
        identity=IdentityManifest(receipt_validator=callback),
    )
    with pytest.raises(RuntimeError) as raised:
        observer.validate_transcript_receipt(payload={}, cwd="/work", expected_session_id=None)
    assert raised.value is error


# --- admit_operator_candidate -----------------------------------------------


def test_operator_admitter_forwards_exact_context_and_value() -> None:
    seen: list[OperatorCandidateContext] = []
    expected = TranscriptCandidate(location="/work/t.jsonl", session_id="s1")
    callback = lambda ctx: (seen.append(ctx), expected)[1]  # noqa: E731
    observer = _observer(
        primary=SourceManifest(factory=_source, channel=_primary_channel()),
        identity=IdentityManifest(operator_candidate_admitter=callback),
    )
    result = observer.admit_operator_candidate(
        cwd="/work", candidate="/work/t.jsonl", domain="d", after=2.0
    )
    assert result == expected
    assert seen == [
        OperatorCandidateContext(cwd="/work", candidate="/work/t.jsonl", domain="d", after=2.0)
    ]


def test_operator_admitter_absent_returns_superclass_refusal() -> None:
    observer = _observer(
        primary=SourceManifest(factory=_source, channel=_primary_channel()),
    )
    with pytest.raises(ValueError, match="has no operator-bindable"):
        observer.admit_operator_candidate(cwd="/work", candidate="/work/t.jsonl")


def test_operator_admitter_wrong_result_type_raises_type_error() -> None:
    callback = lambda _ctx: "not-a-candidate"  # noqa: E731
    observer = _observer(
        primary=SourceManifest(factory=_source, channel=_primary_channel()),
        identity=IdentityManifest(operator_candidate_admitter=callback),
    )
    with pytest.raises(TypeError, match="operator candidate admitter"):
        observer.admit_operator_candidate(cwd="/work", candidate="/work/t.jsonl")


def test_operator_admitter_exception_propagates() -> None:
    error = RuntimeError("admit-boom")
    callback = lambda _ctx: (_ for _ in ()).throw(error)  # noqa: E731
    observer = _observer(
        primary=SourceManifest(factory=_source, channel=_primary_channel()),
        identity=IdentityManifest(operator_candidate_admitter=callback),
    )
    with pytest.raises(RuntimeError) as raised:
        observer.admit_operator_candidate(cwd="/work", candidate="/work/t.jsonl")
    assert raised.value is error


# --- trajectory_capabilities ------------------------------------------------


def test_trajectory_capabilities_survive_compilation() -> None:
    caps = TrajectoryCapabilities()
    observation = ObservationManifest(
        primary=SourceManifest(factory=_source, channel=_primary_channel()),
        screen=ScreenManifest(classifier=_screen),
        trajectory_capabilities=caps,
    )
    harness = compile_manifest(
        "acme",
        HarnessManifest(
            api_version=MANIFEST_API_VERSION,
            binary="acme",
            icon="@",
            launch=LaunchManifest(planner=_plan, approvals=frozenset({"manual"})),
            observation=observation,
        ),
    )
    assert harness.observer.trajectory_capabilities is caps


def test_default_trajectory_capabilities_match_observer_default() -> None:
    observer = _observer(
        primary=SourceManifest(factory=_source, channel=_primary_channel()),
    )
    assert observer.trajectory_capabilities == HarnessObserver.trajectory_capabilities


# --- absent callbacks preserve superclass defaults -------------------------


def test_all_absent_optional_callbacks_preserve_superclass_behaviour() -> None:
    observer = _observer(
        primary=SourceManifest(factory=_source, channel=_primary_channel()),
    )
    assert observer.native_children(Path("/work/t.jsonl")) == []
    assert observer.stream_floor("/work/t.jsonl") is None
    assert observer.transcript_candidates(cwd="/work") == []
    with pytest.raises(ValueError, match="does not implement"):
        observer.validate_transcript_receipt(payload={}, cwd="/work", expected_session_id=None)
    with pytest.raises(ValueError, match="has no operator-bindable"):
        observer.admit_operator_candidate(cwd="/work", candidate="/work/t.jsonl")
