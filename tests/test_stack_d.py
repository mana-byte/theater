"""Transcript identity canonicalisation and receipt validation tests."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from theater.daemon.observer import Observer
from theater.daemon.registry import Registry
from theater.harness.source import (
    Attachment,
    Batch,
    ReceiptAdmission,
    Source,
    SourceContractError,
)
from theater.provenance import TranscriptProvenance

# ---- helpers ---------------------------------------------------------------


class _FakeSource(Source):
    """A minimal source whose admit_exact_location is controllable."""

    def __init__(
        self,
        *,
        admit_result: ReceiptAdmission | None = "accepted",
        known_location: str | None = None,
    ):
        self._admit_result = admit_result
        self._known_location = known_location
        self.path: Path | None = Path(known_location) if known_location else None
        self.committed = False
        self.discarded = False

    async def read(self) -> Batch:
        return Batch()

    def commit_attachment(self) -> None:
        self.committed = True

    def discard_attachment(self) -> None:
        self.discarded = True

    def admit_exact_location(self, *, location: str, session_id: str) -> ReceiptAdmission:
        return self._admit_result  # type: ignore[return-value]


def _make_observer(registry: Registry) -> Observer:
    return Observer(registry, harnesses={})


def _register_participant(
    registry: Registry, *, pid: str = "p1", harness: str = "claude", cwd: str = "/tmp"
):
    p = registry.register(harness=harness, pane=None, cwd=cwd)
    p.id = pid
    registry.store.upsert_participant(p)
    return registry.store.get_participant(pid)


# ---- D1: canonical_location at the observer boundary -----------------------


def test_on_attach_canonicalises_bound_transcripts_key(registry: Registry, tmp_path):
    """_on_attach stores the canonical location as the binding key."""
    observer = _make_observer(registry)
    f = tmp_path / "t.jsonl"
    f.write_text("[]")
    # Simulate a source reporting a path with a redundant .. segment
    raw_location = f"{tmp_path}/sub/../t.jsonl"
    canonical = str(f.resolve())
    assert raw_location != canonical

    attached = Attachment(
        location=raw_location,
        session_id="sess-1",
        correlation=str(TranscriptProvenance.OPERATOR),
    )
    p = _register_participant(registry, pid="p1")
    observer._on_attach(p.id, attached)

    # The binding must be keyed by the canonical location, not the raw one.
    assert canonical in observer._bound_transcripts
    assert raw_location not in observer._bound_transcripts
    assert observer._bound_transcripts[canonical] == "p1"


def test_on_attach_canonicalises_persisted_transcript_location(registry: Registry, tmp_path):
    """The persisted transcript location uses its canonical spelling."""
    observer = _make_observer(registry)
    f = tmp_path / "t.jsonl"
    f.write_text("[]")
    raw_location = f"{tmp_path}/sub/../t.jsonl"
    canonical = str(f.resolve())

    attached = Attachment(
        location=raw_location,
        session_id="sess-1",
        correlation=str(TranscriptProvenance.OPERATOR),
    )
    p = _register_participant(registry, pid="p1")
    observer._on_attach(p.id, attached)

    stored = registry.store.get_participant("p1")
    assert stored.transcript_location == canonical


def test_on_attach_symlink_canonicalises(registry: Registry, tmp_path):
    """A symlinked transcript path resolves to its target."""
    observer = _make_observer(registry)
    target = tmp_path / "real.jsonl"
    target.write_text("[]")
    link = tmp_path / "link.jsonl"
    link.symlink_to(target)

    attached = Attachment(
        location=str(link),
        session_id="sess-1",
        correlation=str(TranscriptProvenance.OPERATOR),
    )
    p = _register_participant(registry, pid="p1")
    observer._on_attach(p.id, attached)

    stored = registry.store.get_participant("p1")
    assert stored.transcript_location == str(target.resolve())


def test_on_attach_opaque_location_not_normalised(registry: Registry):
    """A scheme:// location passes through canonicalisation unchanged."""
    observer = _make_observer(registry)
    opaque = "opencode://ses-abc"
    attached = Attachment(
        location=opaque,
        session_id="sess-1",
        correlation=str(TranscriptProvenance.OPERATOR),
    )
    p = _register_participant(registry, pid="p1")
    observer._on_attach(p.id, attached)

    stored = registry.store.get_participant("p1")
    assert stored.transcript_location == opaque
    assert opaque in observer._bound_transcripts


def test_location_bound_to_another_live_finds_canonical_match(registry: Registry, tmp_path):
    """Live binding lookup matches alternate spellings of the same path."""
    observer = _make_observer(registry)
    f = tmp_path / "t.jsonl"
    f.write_text("[]")
    canonical = str(f.resolve())
    raw = f"{tmp_path}/./t.jsonl"

    attached = Attachment(
        location=canonical,
        session_id="sess-1",
        correlation=str(TranscriptProvenance.OPERATOR),
    )
    p1 = _register_participant(registry, pid="p1")
    observer._on_attach(p1.id, attached)

    assert observer._location_bound_to_another_live("p2", raw)


def test_trusted_dead_owner_blocks_with_path_alias(registry: Registry, tmp_path):
    """A trusted dead owner blocks aliases of its transcript path."""
    observer = _make_observer(registry)
    f = tmp_path / "t.jsonl"
    f.write_text("[]")
    canonical = str(f.resolve())
    alias = f"{tmp_path}/sub/../t.jsonl"

    dead = registry.register(harness="claude", pane=None, cwd="/tmp")
    dead.session_correlation = str(TranscriptProvenance.OPERATOR)
    dead.transcript_location = alias
    dead.session_id = "sess-dead"
    registry.store.upsert_participant(dead)
    registry.mark_dead(dead.id)

    attached = Attachment(
        location=canonical,
        session_id="sess-new",
        correlation=str(TranscriptProvenance.OPERATOR),
    )
    assert observer._trusted_dead_owner_blocks("different-pid", attached)


def test_is_untrusted_rotation_uses_same_location(registry: Registry, tmp_path):
    """A path alias of the owned transcript is not an untrusted rotation."""
    observer = _make_observer(registry)
    f = tmp_path / "t.jsonl"
    f.write_text("[]")
    canonical = str(f.resolve())
    alias = f"{tmp_path}/sub/../t.jsonl"

    p = _register_participant(registry, pid="p1")
    p.transcript_location = canonical
    p.session_correlation = str(TranscriptProvenance.OPERATOR)
    registry.store.upsert_participant(p)

    attached = Attachment(
        location=alias,
        session_id="sess-1",
        correlation=str(TranscriptProvenance.HEURISTIC),
    )
    assert not observer._is_untrusted_rotation(p.id, attached)


def test_history_correlation_uses_same_location(registry: Registry, tmp_path):
    """History ambiguity treats path aliases as the same transcript."""
    from theater.daemon.observer import history_correlation_is_ambiguous
    from theater.harness.source import History

    f = tmp_path / "t.jsonl"
    f.write_text("[]")
    canonical = str(f.resolve())
    alias = f"{tmp_path}/sub/../t.jsonl"

    live = registry.register(harness="claude", pane=None, cwd="/tmp")
    live.transcript_location = canonical
    live.session_correlation = str(TranscriptProvenance.HEURISTIC)
    registry.store.upsert_participant(live)

    dead = registry.register(harness="claude", pane=None, cwd="/tmp")
    dead.transcript_location = alias
    dead.session_correlation = str(TranscriptProvenance.OPERATOR)
    registry.store.upsert_participant(dead)
    registry.mark_dead(dead.id)

    history = History(
        location=canonical,
        correlation=str(TranscriptProvenance.HEURISTIC),
    )
    assert history_correlation_is_ambiguous(registry, live.id, history)


def test_stage_receipt_source_canonicalises_binding_key(registry: Registry, tmp_path):
    """Accepted receipts key binding state by canonical location."""
    observer = _make_observer(registry)
    f = tmp_path / "t.jsonl"
    f.write_text("[]")
    canonical = str(f.resolve())
    raw = f"{tmp_path}/sub/../t.jsonl"

    p = _register_participant(registry, pid="p1")
    source = _FakeSource(admit_result="accepted", known_location=canonical)
    observer._register_source(p.id, source)

    observer._stage_receipt_source(p.id, source, location=raw, session_id="sess-1")

    assert canonical in observer._binding_correlation
    assert canonical in observer._binding_sessions
    assert raw not in observer._binding_correlation


# ---- D2: ReceiptAdmission validation ---------------------------------------


def test_stage_receipt_source_rejects_none(registry: Registry, tmp_path):
    """A source returning None from admission raises SourceContractError."""
    observer = _make_observer(registry)
    p = _register_participant(registry, pid="p1")
    f = tmp_path / "t.jsonl"
    f.write_text("[]")
    source = _FakeSource(admit_result=None, known_location=str(f))  # type: ignore[arg-type]
    observer._register_source(p.id, source)

    with pytest.raises(SourceContractError, match="admit_exact_location"):
        observer._stage_receipt_source(p.id, source, location=str(f), session_id="sess-1")

    assert not observer._bound_transcripts
    assert not observer._binding_correlation
    assert not observer._binding_sessions


def test_stage_receipt_source_rejects_bogus_string(registry: Registry, tmp_path):
    """A source returning an unknown admission raises SourceContractError."""
    observer = _make_observer(registry)
    p = _register_participant(registry, pid="p1")
    f = tmp_path / "t.jsonl"
    f.write_text("[]")
    source = _FakeSource(admit_result="bogus", known_location=str(f))  # type: ignore[arg-type]
    observer._register_source(p.id, source)

    with pytest.raises(SourceContractError, match="admit_exact_location"):
        observer._stage_receipt_source(p.id, source, location=str(f), session_id="sess-1")

    assert not observer._bound_transcripts
    assert not observer._binding_correlation


def test_stage_receipt_source_error_message_names_source_and_method(registry: Registry, tmp_path):
    """The error message must name the source class and the method, and tell
    a plugin author what to return instead.
    """
    observer = _make_observer(registry)
    p = _register_participant(registry, pid="p1")
    f = tmp_path / "t.jsonl"
    f.write_text("[]")
    source = _FakeSource(admit_result=None, known_location=str(f))  # type: ignore[arg-type]
    observer._register_source(p.id, source)

    with pytest.raises(SourceContractError) as exc_info:
        observer._stage_receipt_source(p.id, source, location=str(f), session_id="sess-1")

    msg = str(exc_info.value)
    assert "_FakeSource" in msg
    assert "admit_exact_location" in msg
    assert "accepted" in msg
    assert "staged" in msg


# ---- D3: alias-stored participant rows --------------------------------------


def _alias_predecessor(registry, *, alias, canonical, session_id, live=False, pane=None):
    """Persist a participant under an alias spelling, bypassing registration."""
    p = registry.register(
        harness=canonical,
        pane=pane,
        cwd="/tmp",
        session_id=session_id,
    )
    p.harness = alias
    p.session_correlation = "exact"
    registry.store.upsert_participant(p)
    stored = registry.store.get_participant(p.id)
    assert stored.harness == alias, (
        f"expected alias {alias!r} in store, got {stored.harness!r}; "
        "registry.register normalized the harness and the test is hollow"
    )
    if not live:
        registry.mark_dead(p.id)
    return p


def test_alias_converges_on_reconnect(registry: Registry):
    """A claimed-id reconnect rewrites a stored alias to canonical form."""
    p = _alias_predecessor(
        registry, alias="claude-code", canonical="claude", session_id="sess-1", live=True
    )
    assert registry.store.get_participant(p.id).harness == "claude-code"

    registry.register(
        harness="claude",
        pane=None,
        cwd="/tmp",
        session_id="sess-1-updated",
        claimed_id=p.id,
    )

    stored = registry.store.get_participant(p.id)
    assert stored.harness == "claude", (
        f"alias did not converge on reconnect; stored harness is {stored.harness!r}"
    )


def test_alias_converges_even_when_incoming_is_alias(registry: Registry):
    """A reconnect with an alias spelling still converges to canonical form."""
    p = _alias_predecessor(
        registry, alias="claude-code", canonical="claude", session_id="sess-1", live=True
    )
    registry.register(
        harness="claude-code",
        pane=None,
        cwd="/tmp",
        session_id="sess-1",
        claimed_id=p.id,
    )
    stored = registry.store.get_participant(p.id)
    assert stored.harness == "claude"


def test_observer_has_cwd_competitor_finds_alias_peer(registry: Registry):
    """Cwd competitor lookup finds peers stored under a harness alias."""
    observer = _make_observer(registry)
    p1 = registry.register(harness="claude", pane=None, cwd="/tmp")
    _alias_predecessor(
        registry, alias="claude-code", canonical="claude", session_id="sess-2", live=True
    )
    assert observer._has_cwd_competitor(p1.id, collision_domain=None)


def test_observer_shares_transcript_location_finds_alias_peer(registry: Registry, tmp_path):
    """History ambiguity finds dead peers stored under a harness alias."""
    from theater.daemon.observer import history_correlation_is_ambiguous
    from theater.harness.source import History

    f = tmp_path / "t.jsonl"
    f.write_text("[]")
    canonical_loc = str(f.resolve())

    live = registry.register(harness="claude", pane=None, cwd="/tmp")
    live.transcript_location = canonical_loc
    live.session_correlation = str(TranscriptProvenance.HEURISTIC)
    registry.store.upsert_participant(live)

    dead = _alias_predecessor(
        registry, alias="claude-code", canonical="claude", session_id="sess-dead"
    )
    dead.transcript_location = canonical_loc
    registry.store.upsert_participant(dead)

    history = History(
        location=canonical_loc,
        correlation=str(TranscriptProvenance.HEURISTIC),
    )
    assert history_correlation_is_ambiguous(registry, live.id, history)


def test_methods_reject_cross_participant_receipt_finds_alias(registry: Registry, tmp_path):
    """Receipt ownership checks find owners stored under a harness alias."""
    from theater.daemon.methods import _reject_cross_participant_receipt
    from theater.models import BadRequest

    f = tmp_path / "t.jsonl"
    f.write_text("[]")
    loc = str(f.resolve())

    other = _alias_predecessor(
        registry, alias="claude-code", canonical="claude", session_id="sess-other"
    )
    other.transcript_location = loc
    other.session_correlation = str(TranscriptProvenance.OPERATOR)
    registry.store.upsert_participant(other)

    subject = registry.register(harness="claude", pane=None, cwd="/tmp", session_id="sess-me")

    daemon = SimpleNamespace(registry=registry)

    with pytest.raises(BadRequest, match="already owned"):
        _reject_cross_participant_receipt(
            daemon,
            participant_id=subject.id,
            harness="claude",
            session_id="sess-me",
            transcript_location=loc,
        )


def test_methods_receipt_clears_identity_loss_quarantine(registry: Registry, tmp_path):
    """An accepted receipt from an alias-stored participant clears quarantine."""
    import asyncio

    from theater.daemon.methods import _transcript_receipt
    from theater.harness.source import TranscriptCandidate
    from theater.transcript_identity import TRANSCRIPT_IDENTITY_LOST_CODE

    p = _alias_predecessor(
        registry, alias="claude-code", canonical="claude", session_id="sess-1", live=True
    )
    registry.store.set_receipt_token(p.id, "tok")
    transcript = tmp_path / "sess-1.jsonl"
    transcript.write_text("[]")
    location = str(transcript)

    from theater.daemon.observer import Observer

    class _FakeObserver:
        def validate_transcript_receipt(self, *, payload, cwd, expected_session_id):
            return TranscriptCandidate(location=location, session_id="sess-1")

    class _FakeHarness:
        observer = _FakeObserver()

    observer = Observer(registry, harnesses={"claude": _FakeHarness()})
    source = _FakeSource(admit_result="accepted", known_location=location)
    observer._register_source(p.id, source)
    daemon = SimpleNamespace(
        registry=registry,
        store=registry.store,
        observer=observer,
    )
    registry.store.bus_append(
        "agent.observation_error",
        to_id=p.id,
        payload={"code": TRANSCRIPT_IDENTITY_LOST_CODE},
    )
    assert registry.store.observation_error_active(p.id, TRANSCRIPT_IDENTITY_LOST_CODE)

    result = asyncio.run(
        _transcript_receipt(
            daemon,
            {
                "id": p.id,
                "token": "tok",
                "payload": {"session_id": "sess-1", "path": location},
            },
        )
    )
    assert result["ok"] is True
    assert result["admission"] == "accepted"
    assert not registry.store.observation_error_active(p.id, TRANSCRIPT_IDENTITY_LOST_CODE)


# ---- E1: _register_source must be inside the try/finally --------------------


class _RaisingAdmitSource(Source):
    """A source whose admit_exact_location returns garbage, with aclose tracking."""

    def __init__(self):
        self.closed = False

    async def read(self) -> Batch:
        return Batch()

    def admit_exact_location(self, *, location: str, session_id: str) -> ReceiptAdmission:
        return "bogus"  # type: ignore[return-value]

    async def aclose(self) -> None:
        self.closed = True


class _RaisingAdmitObserver:
    """An observer that returns the raising source."""

    has_transcript = True

    def __init__(self, source):
        self.source = source

    def open_source(self, *, cwd, session_id=None, after=None):
        return self.source

    def is_idle_screen(self, capture: str) -> bool:
        return False


class _RaisingAdmitHarness:
    """Harness shell that carries the observer."""

    binary = "raising"

    def __init__(self, observer):
        self.observer = observer


async def test_register_source_inside_try_closes_source_on_raise(registry: Registry):
    """A source-contract failure during registration closes the source."""
    source = _RaisingAdmitSource()
    harness = _RaisingAdmitHarness(_RaisingAdmitObserver(source))
    observer = Observer(registry, {"raising": harness}, poll=0.01, search=0.01, sync=0.01)
    p = registry.register(harness="raising", pane=None, cwd="/tmp")
    observer._receipt_candidates[p.id] = ("/tmp/sess-1.jsonl", "sess-1")
    await observer._watch(p.id, "raising")
    assert p.id not in observer._sources
    assert source.closed


async def test_register_source_contract_error_logs_retirement(registry: Registry, caplog):
    """A source-contract failure logs retirement without escaping the watcher."""
    import logging

    source = _RaisingAdmitSource()
    harness = _RaisingAdmitHarness(_RaisingAdmitObserver(source))
    observer = Observer(registry, {"raising": harness}, poll=0.01, search=0.01, sync=0.01)
    p = registry.register(harness="raising", pane=None, cwd="/tmp")
    observer._receipt_candidates[p.id] = ("/tmp/sess-1.jsonl", "sess-1")

    with caplog.at_level(logging.ERROR, logger="theater.observer"):
        await observer._watch(p.id, "raising")

    assert any(
        "source contract failed" in record.message and "retiring" in record.message
        for record in caplog.records
    ), [r.message for r in caplog.records]
    assert p.id not in observer._sources
    assert source.closed


# ---- E2: convergence guard narrowed ----------------------------------------


def test_alias_converges_on_reconnect_with_narrow_guard(registry: Registry):
    """An alias converges when a participant reconnects."""
    p = _alias_predecessor(
        registry, alias="claude-code", canonical="claude", session_id="sess-1", live=True
    )
    registry.register(
        harness="claude",
        pane=None,
        cwd="/tmp",
        session_id="sess-1",
        claimed_id=p.id,
    )
    stored = registry.store.get_participant(p.id)
    assert stored.harness == "claude"


def test_different_harness_does_not_overwrite(registry: Registry):
    """A different harness cannot overwrite the stored harness on reconnect."""
    p = registry.register(harness="claude", pane=None, cwd="/tmp", session_id="sess-1")
    registry.register(
        harness="codex",
        pane=None,
        cwd="/tmp",
        session_id="sess-1",
        claimed_id=p.id,
    )
    stored = registry.store.get_participant(p.id)
    assert stored.harness == "claude", (
        f"different harness overwrote the row; got {stored.harness!r}"
    )


def test_unknown_harness_does_not_overwrite(registry: Registry):
    """An unknown harness cannot overwrite the stored harness on reconnect."""
    p = registry.register(harness="claude", pane=None, cwd="/tmp", session_id="sess-1")
    registry.register(
        harness="typo-harness",
        pane=None,
        cwd="/tmp",
        session_id="sess-1",
        claimed_id=p.id,
    )
    stored = registry.store.get_participant(p.id)
    assert stored.harness == "claude"


# ---- E3: _confirm_identity_loss uses canonical comparison -------------------


def test_confirm_identity_loss_counts_path_alias_as_same(registry: Registry, tmp_path):
    """Identity-loss evidence accumulates across aliases of the same path."""
    from theater.daemon.observer import IDENTITY_LOSS_CONFIRMATIONS
    from theater.harness.source import IdentityLossEvidence

    observer = _make_observer(registry)
    f = tmp_path / "t.jsonl"
    f.write_text("[]")
    canonical = str(f.resolve())
    alias = f"{tmp_path}/sub/../t.jsonl"

    assert not observer._confirm_identity_loss("p1", IdentityLossEvidence(location=canonical))
    assert IDENTITY_LOSS_CONFIRMATIONS == 2
    assert observer._confirm_identity_loss("p1", IdentityLossEvidence(location=alias))


def test_confirm_identity_loss_different_locations_reset(registry: Registry, tmp_path):
    """Different identity-loss evidence locations reset confirmation."""
    from theater.daemon.observer import IDENTITY_LOSS_CONFIRMATIONS
    from theater.harness.source import IdentityLossEvidence

    observer = _make_observer(registry)
    a = tmp_path / "a.jsonl"
    a.write_text("[]")
    b = tmp_path / "b.jsonl"
    b.write_text("[]")

    observer._confirm_identity_loss("p1", IdentityLossEvidence(location=str(a)))
    if IDENTITY_LOSS_CONFIRMATIONS > 1:
        assert not observer._confirm_identity_loss("p1", IdentityLossEvidence(location=str(b)))


# ---- E4: opportunistic convergence of transcript_location --------------------


def test_on_attach_converges_non_canonical_stored_location(registry: Registry, tmp_path):
    """Attachment rewrites a stored path alias to its canonical spelling."""
    observer = _make_observer(registry)
    f = tmp_path / "t.jsonl"
    f.write_text("[]")
    canonical = str(f.resolve())
    alias = f"{tmp_path}/sub/../t.jsonl"

    p = _register_participant(registry, pid="p1")
    p.transcript_location = alias
    registry.store.upsert_participant(p)

    attached = Attachment(
        location=canonical,
        session_id="sess-1",
        correlation=str(TranscriptProvenance.OPERATOR),
    )
    observer._on_attach(p.id, attached)

    stored = registry.store.get_participant(p.id)
    assert stored.transcript_location == canonical, (
        f"non-canonical spelling survived; got {stored.transcript_location!r}"
    )
