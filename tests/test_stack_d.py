"""Stack D: transcript identity canonicalisation and receipt validation tests.

D1 — canonical transcript-location identity:
    Two spellings of one path (``..`` segment, symlink, ``~``) must collide
    correctly at every observer guard that previously compared byte-for-byte.
    An opaque ``scheme://`` location must never be path-normalised.  A legacy
    non-canonical ``transcript_location`` in the store must still match a
    canonical incoming location.

D2 — ReceiptAdmission validation:
    A source whose ``admit_exact_location()`` returns ``None`` or a bogus
    string must raise ``SourceContractError`` and must not persist a binding
    or emit an accepted receipt event.

D3 — alias-stored participant rows:
    A row stored with an alias harness spelling must be found by every site
    that normalises on read, and must converge to canonical after a reconnect.
"""

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
    """_on_attach stores the canonical location as the dict key.

    Mutation: revert canonical_location(attached.location) to attached.location
    in _on_attach. This test fails because the dict key no longer matches the
    canonicalised lookup in _location_bound_to_another_live.
    """
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
    """The persisted transcript_location must be the canonical spelling.

    Mutation: revert _on_attach to store attached.location instead of loc.
    This test fails because the stored value still has the .. segment.
    """
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
    """A symlinked path is resolved to its target.

    Mutation: revert canonical_location to not resolve symlinks. This test
    fails because the stored path is the symlink, not the target.
    """
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
    """A scheme:// location must pass through canonicalisation unchanged.

    Mutation: if canonical_location started expanduser/resolving opaque
    locations, this test fails because the stored value is mangled.
    """
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
    """_location_bound_to_another_live must find a binding even when the
    incoming location is a different spelling of the same file.

    Mutation: revert canonical_location(location) in the dict lookup to
    a raw lookup. This test fails because the raw key misses.
    """
    observer = _make_observer(registry)
    f = tmp_path / "t.jsonl"
    f.write_text("[]")
    canonical = str(f.resolve())
    raw = f"{tmp_path}/./t.jsonl"

    # Bind participant p1 to the canonical path
    attached = Attachment(
        location=canonical,
        session_id="sess-1",
        correlation=str(TranscriptProvenance.OPERATOR),
    )
    p1 = _register_participant(registry, pid="p1")
    observer._on_attach(p1.id, attached)

    # Now check if a different spelling of the same file is bound to p1
    assert observer._location_bound_to_another_live("p2", raw)


def test_trusted_dead_owner_blocks_with_path_alias(registry: Registry, tmp_path):
    """_trusted_dead_owner_blocks must block when the dead owner's stored
    location is a different spelling of the same file.

    Mutation: revert same_location to raw != in _trusted_dead_owner_blocks.
    This test fails because the alias-spelled dead owner is not found.
    """
    observer = _make_observer(registry)
    f = tmp_path / "t.jsonl"
    f.write_text("[]")
    canonical = str(f.resolve())
    alias = f"{tmp_path}/sub/../t.jsonl"

    # Create a dead participant with a non-canonical stored location
    dead = registry.register(harness="claude", pane=None, cwd="/tmp")
    dead.session_correlation = str(TranscriptProvenance.OPERATOR)
    dead.transcript_location = alias
    dead.session_id = "sess-dead"
    registry.store.upsert_participant(dead)
    registry.mark_dead(dead.id)

    # A different participant trying to attach the canonical spelling of
    # the same file must be blocked by the dead owner.
    attached = Attachment(
        location=canonical,
        session_id="sess-new",
        correlation=str(TranscriptProvenance.OPERATOR),
    )
    assert observer._trusted_dead_owner_blocks("different-pid", attached)


def test_is_untrusted_rotation_uses_same_location(registry: Registry, tmp_path):
    """_is_untrusted_rotation must return False when the attached location is
    a different spelling of the same file the participant already owns.

    Mutation: revert same_location to raw !=. This test fails because a
    rotation is incorrectly detected.
    """
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
    # Same file, so this is NOT an untrusted rotation
    assert not observer._is_untrusted_rotation(p.id, attached)


def test_history_correlation_uses_same_location(registry: Registry, tmp_path):
    """history_correlation_is_ambiguous must not flag a history read as
    ambiguous when a dead participant's stored location is a different
    spelling of the same file.

    Mutation: revert same_location to raw != in
    history_correlation_is_ambiguous. This test fails because the dead
    participant appears to own a *different* transcript.
    """
    from theater.daemon.observer import history_correlation_is_ambiguous
    from theater.harness.source import History

    f = tmp_path / "t.jsonl"
    f.write_text("[]")
    canonical = str(f.resolve())
    alias = f"{tmp_path}/sub/../t.jsonl"

    # Live participant with the canonical location
    live = registry.register(harness="claude", pane=None, cwd="/tmp")
    live.transcript_location = canonical
    live.session_correlation = str(TranscriptProvenance.HEURISTIC)
    registry.store.upsert_participant(live)

    # Dead participant with the alias spelling of the same file
    dead = registry.register(harness="claude", pane=None, cwd="/tmp")
    dead.transcript_location = alias
    dead.session_correlation = str(TranscriptProvenance.OPERATOR)
    registry.store.upsert_participant(dead)
    registry.mark_dead(dead.id)

    history = History(
        location=canonical,
        correlation=str(TranscriptProvenance.HEURISTIC),
    )
    # The dead participant owns the same file (different spelling), so the
    # history read IS ambiguous.
    assert history_correlation_is_ambiguous(registry, live.id, history)


def test_stage_receipt_source_canonicalises_binding_key(registry: Registry, tmp_path):
    """_stage_receipt_source must key the binding dicts by the canonical
    location, not the raw receipt location.

    Mutation: revert canonical_location(location) in _stage_receipt_source.
    This test fails because the binding key is the raw path, not the
    canonical one, and the lookup in _location_bound_to_another_live misses.
    """
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
    """A source returning None from admit_exact_location must raise
    SourceContractError, not silently succeed.

    Mutation: remove the validation in _stage_receipt_source. This test
    fails because None falls through without raising, and the caller
    gets ok: true with admission: null.
    """
    observer = _make_observer(registry)
    p = _register_participant(registry, pid="p1")
    f = tmp_path / "t.jsonl"
    f.write_text("[]")
    source = _FakeSource(admit_result=None, known_location=str(f))  # type: ignore[arg-type]
    observer._register_source(p.id, source)

    with pytest.raises(SourceContractError, match="admit_exact_location"):
        observer._stage_receipt_source(p.id, source, location=str(f), session_id="sess-1")

    # No binding must have been persisted
    assert not observer._bound_transcripts
    assert not observer._binding_correlation
    assert not observer._binding_sessions


def test_stage_receipt_source_rejects_bogus_string(registry: Registry, tmp_path):
    """A source returning a bogus string must raise SourceContractError.

    Mutation: remove the validation. This test fails because the bogus
    string is accepted and persisted.
    """
    observer = _make_observer(registry)
    p = _register_participant(registry, pid="p1")
    f = tmp_path / "t.jsonl"
    f.write_text("[]")
    source = _FakeSource(admit_result="bogus", known_location=str(f))  # type: ignore[arg-type]
    observer._register_source(p.id, source)

    with pytest.raises(SourceContractError, match="admit_exact_location"):
        observer._stage_receipt_source(p.id, source, location=str(f), session_id="sess-1")

    # No binding must have been persisted
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
    """Create a participant whose stored harness is an alias spelling.

    ``registry.register`` normalizes the harness at line 243, so calling it
    with ``harness="claude-code"`` stores ``"claude"`` and defeats the test.
    To get a genuinely alias-stored row we register under the canonical name,
    then mutate ``p.harness`` to the alias and re-upsert — bypassing the
    normalizer. Assert the stored spelling is the alias before returning.
    """
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
    """A reconnect via claimed_id must rewrite the stored alias to canonical.

    Mutation: remove the ``if existing.harness != harness`` convergence in
    registry.register's claimed_id branch. This test fails because the stored
    harness stays as the alias after reconnect.
    """
    # Plant an alias-stored row
    p = _alias_predecessor(
        registry, alias="claude-code", canonical="claude", session_id="sess-1", live=True
    )
    assert registry.store.get_participant(p.id).harness == "claude-code"

    # Reconnect with the canonical name and the claimed id
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
    """A reconnect with an alias spelling must also converge, because
    register() normalises the incoming harness before comparing."""
    p = _alias_predecessor(
        registry, alias="claude-code", canonical="claude", session_id="sess-1", live=True
    )
    # Reconnect with the alias spelling itself — register normalises it
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
    """_has_cwd_competitor must find a peer whose harness is stored as an
    alias, because it normalises on read.

    Mutation: revert normalize_harness(other.harness) to other.harness in
    _has_cwd_competitor. This test fails because the alias-stored peer is
    not found and the competitor check returns False.
    """
    observer = _make_observer(registry)
    # A live participant with the canonical name
    p1 = registry.register(harness="claude", pane=None, cwd="/tmp")
    # A live peer with the alias spelling
    _alias_predecessor(
        registry, alias="claude-code", canonical="claude", session_id="sess-2", live=True
    )
    assert observer._has_cwd_competitor(p1.id, collision_domain=None)


def test_observer_shares_transcript_location_finds_alias_peer(registry: Registry, tmp_path):
    """history_correlation_is_ambiguous must find a dead participant whose
    harness is stored as an alias, because the harness comparison normalises
    on read.

    Mutation: revert normalize_harness(other.harness) to other.harness in
    history_correlation_is_ambiguous. This test fails because the alias-stored
    dead participant is skipped.
    """
    from theater.daemon.observer import history_correlation_is_ambiguous
    from theater.harness.source import History

    f = tmp_path / "t.jsonl"
    f.write_text("[]")
    canonical_loc = str(f.resolve())

    live = registry.register(harness="claude", pane=None, cwd="/tmp")
    live.transcript_location = canonical_loc
    live.session_correlation = str(TranscriptProvenance.HEURISTIC)
    registry.store.upsert_participant(live)

    # Dead participant with alias harness and the same location
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
    """_reject_cross_participant_receipt must find an alias-stored participant
    that owns the same transcript location.

    Mutation: revert normalize(other.harness) to other.harness in
    _reject_cross_participant_receipt. This test fails because the
    alias-stored participant is not found and the cross-participant check
    passes silently.
    """
    from theater.daemon.methods import _reject_cross_participant_receipt
    from theater.models import BadRequest

    f = tmp_path / "t.jsonl"
    f.write_text("[]")
    loc = str(f.resolve())

    # Plant an alias-stored participant owning the location
    other = _alias_predecessor(
        registry, alias="claude-code", canonical="claude", session_id="sess-other"
    )
    other.transcript_location = loc
    other.session_correlation = str(TranscriptProvenance.OPERATOR)
    registry.store.upsert_participant(other)

    # The subject participant, canonical spelling
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


def test_methods_receipt_observer_lookup_finds_alias(registry: Registry):
    """The observer harness lookup in the receipt handler must normalise the
    stored harness before looking it up in the daemon-local harnesses dict.

    Mutation: revert normalize(participant.harness) to participant.harness
    in the receipt handler. This test fails because the alias-stored
    participant's harness misses the dict lookup.
    """
    import asyncio

    from theater.daemon.methods import _transcript_receipt
    from theater.harness.source import TranscriptCandidate

    p = _alias_predecessor(
        registry, alias="claude-code", canonical="claude", session_id="sess-1", live=True
    )
    registry.store.set_receipt_token(p.id, "tok")

    # Build a daemon with a harness registered under the canonical name
    from theater.daemon.observer import Observer

    class _FakeObserver:
        def validate_transcript_receipt(self, *, payload, cwd, expected_session_id):
            return TranscriptCandidate(location="/tmp/sess-1.jsonl", session_id="sess-1")

    class _FakeHarness:
        observer = _FakeObserver()

    observer = Observer(registry, harnesses={"claude": _FakeHarness()})
    daemon = SimpleNamespace(
        registry=registry,
        store=registry.store,
        observer=observer,
    )

    result = asyncio.run(
        _transcript_receipt(
            daemon,
            {
                "id": p.id,
                "token": "tok",
                "payload": {"session_id": "sess-1", "path": "/tmp/sess-1.jsonl"},
            },
        )
    )
    assert result["ok"] is True


# ---- D4: harness-neutral constant naming ------------------------------------


def test_transcript_receipt_bus_kind_constant_exists():
    """The renamed constant must exist and have the correct wire value.

    Mutation: rename it back to CLAUDE_RECEIPT_BUS_KIND. This test fails
    because the old name no longer exists.
    """
    from theater.daemon.methods import TRANSCRIPT_RECEIPT_BUS_KIND

    assert TRANSCRIPT_RECEIPT_BUS_KIND == "agent.transcript_receipt"
