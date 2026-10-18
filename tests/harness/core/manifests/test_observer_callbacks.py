"""A compiled manifest observer forwards each optional callback, or defers to the base class."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
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
    primary: SourceManifest | None = ...,
    harness_name: str | None = None,
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
    return ManifestHarnessObserver(observation, harness_name=harness_name)


@dataclass(frozen=True)
class _Hook:
    """One optional observer callback: where it is declared and how it is called."""

    section: str
    field: str
    call: Callable[[ManifestHarnessObserver], object]
    context: object
    expected: object
    bad: object
    bad_match: str
    absent: Callable[[ManifestHarnessObserver], None]


def _absent_value(call, value):
    def check(observer: ManifestHarnessObserver) -> None:
        result = call(observer)
        assert result is None if value is None else result == value

    return check


def _absent_refusal(call, match, *, supports_receipts: bool | None = None):
    def check(observer: ManifestHarnessObserver) -> None:
        with pytest.raises(ValueError, match=match):
            call(observer)
        if supports_receipts is not None:
            assert observer.supports_transcript_receipts is supports_receipts

    return check


_CANDIDATE = TranscriptCandidate(location="/work/t.jsonl", session_id="s1")
_CALL_NATIVE_CHILDREN = lambda o: o.native_children(Path("/work/t.jsonl"))  # noqa: E731
_CALL_STREAM_FLOOR = lambda o: o.stream_floor("/work/t.jsonl")  # noqa: E731
_CALL_CANDIDATES = lambda o: o.transcript_candidates(cwd="/work", domain="d", after=1.0)  # noqa: E731
_CALL_RECEIPT = lambda o: o.validate_transcript_receipt(  # noqa: E731
    payload={"key": "value"}, cwd="/work", expected_session_id="s1"
)
_CALL_OPERATOR = lambda o: o.admit_operator_candidate(  # noqa: E731
    cwd="/work", candidate="/work/t.jsonl", domain="d", after=2.0
)

_HOOKS = {
    "native_children": _Hook(
        section="lineage",
        field="native_children",
        call=_CALL_NATIVE_CHILDREN,
        context=NativeChildrenContext(transcript=Path("/work/t.jsonl")),
        expected=[NativeChild(session_id="child-1")],
        bad=[object()],
        bad_match="native children",
        absent=_absent_value(_CALL_NATIVE_CHILDREN, []),
    ),
    "stream_floor": _Hook(
        section="identity",
        field="stream_floor",
        call=_CALL_STREAM_FLOOR,
        context=StreamFloorContext(location="/work/t.jsonl"),
        expected=StreamPoint(records=10, size=500),
        bad="not-a-stream-point",
        bad_match="stream-floor",
        absent=_absent_value(_CALL_STREAM_FLOOR, None),
    ),
    "transcript_candidates": _Hook(
        section="identity",
        field="transcript_candidates",
        call=_CALL_CANDIDATES,
        context=TranscriptCandidatesContext(cwd="/work", domain="d", after=1.0),
        expected=[_CANDIDATE],
        bad=[object()],
        bad_match="transcript candidates",
        absent=_absent_value(_CALL_CANDIDATES, []),
    ),
    "receipt_validator": _Hook(
        section="identity",
        field="receipt_validator",
        call=_CALL_RECEIPT,
        context=ReceiptValidationContext(
            payload={"key": "value"}, cwd="/work", expected_session_id="s1"
        ),
        expected=_CANDIDATE,
        bad="not-a-candidate",
        bad_match="receipt validator",
        absent=_absent_refusal(
            _CALL_RECEIPT, r"harness 'acme'.*does not implement", supports_receipts=False
        ),
    ),
    "operator_candidate_admitter": _Hook(
        section="identity",
        field="operator_candidate_admitter",
        call=_CALL_OPERATOR,
        context=OperatorCandidateContext(
            cwd="/work", candidate="/work/t.jsonl", domain="d", after=2.0
        ),
        expected=_CANDIDATE,
        bad="not-a-candidate",
        bad_match="operator candidate admitter",
        absent=_absent_refusal(_CALL_OPERATOR, "has no operator-bindable"),
    ),
}
_HOOK = pytest.mark.parametrize("hook", list(_HOOKS.values()), ids=list(_HOOKS))


def _hooked(hook: _Hook, callback) -> ManifestHarnessObserver:
    section = {"lineage": LineageManifest, "identity": IdentityManifest}[hook.section]
    return _observer(
        primary=SourceManifest(factory=_source, channel=_primary_channel()),
        harness_name="acme",
        **{hook.section: section(**{hook.field: callback})},
    )


@_HOOK
def test_callback_forwards_exact_context_and_value(hook: _Hook) -> None:
    seen: list[object] = []
    observer = _hooked(hook, lambda ctx: (seen.append(ctx), hook.expected)[1])
    assert hook.call(observer) == hook.expected
    assert seen == [hook.context]


@_HOOK
def test_absent_callback_returns_the_superclass_default(hook: _Hook) -> None:
    observer = _observer(
        primary=SourceManifest(factory=_source, channel=_primary_channel()),
        harness_name="acme",
    )
    hook.absent(observer)


@_HOOK
def test_callback_wrong_result_type_raises_type_error(hook: _Hook) -> None:
    observer = _hooked(hook, lambda _ctx: hook.bad)
    with pytest.raises(TypeError, match=hook.bad_match):
        hook.call(observer)


@_HOOK
def test_callback_exception_propagates(hook: _Hook) -> None:
    error = RuntimeError(f"{hook.field}-boom")
    observer = _hooked(hook, lambda _ctx: (_ for _ in ()).throw(error))
    with pytest.raises(RuntimeError) as raised:
        hook.call(observer)
    assert raised.value is error


def test_stream_floor_none_return_is_accepted() -> None:
    observer = _hooked(_HOOKS["stream_floor"], lambda _ctx: None)
    assert observer.stream_floor("/work/t.jsonl") is None


def test_receipt_validation_context_payload_is_immutable_copy() -> None:
    original: dict[str, object] = {"key": "value"}
    ctx = ReceiptValidationContext(payload=original, cwd=None, expected_session_id=None)
    original["key"] = "changed"
    assert ctx.payload["key"] == "value"
    with pytest.raises(TypeError):
        ctx.payload["key"] = "mutate"  # type: ignore[index]


def test_receipt_validator_declares_support() -> None:
    observer = _hooked(_HOOKS["receipt_validator"], lambda _ctx: _CANDIDATE)
    assert observer.supports_transcript_receipts is True


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
            launch=LaunchManifest(planner=_plan, approvals=("manual",)),
            observation=observation,
        ),
    )
    assert harness.observer.trajectory_capabilities is caps


def test_default_trajectory_capabilities_match_observer_default() -> None:
    observer = _observer(
        primary=SourceManifest(factory=_source, channel=_primary_channel()),
    )
    assert observer.trajectory_capabilities == HarnessObserver.trajectory_capabilities
