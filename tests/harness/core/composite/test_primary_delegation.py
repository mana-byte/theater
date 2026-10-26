"""A composite source delegates attachment, history and identity hooks to its primary."""

from __future__ import annotations

import pytest

from tests.support.composite_sources import ControlledSource, RecordingSource, decl, make_fact
from theater.harness.channels import CompositeSource, EnrichmentBinding
from theater.harness.contracts.source import (
    Attachment,
    Batch,
    History,
    HistoryPage,
    IdentityLossEvidence,
    Source,
    SourceContractError,
)


class _PrimaryWithAttachment(Source):
    collision_domain = "domain"

    def __init__(self) -> None:
        self.committed = False
        self.discarded = False
        self.revoked = False
        self.admitted: tuple[str, str] | None = None
        self.history_page_call: tuple[str | None, str | None, int, bool] | None = None
        self._batch = Batch(attached=Attachment(location="/work/t.jsonl"))

    async def read(self) -> Batch:
        return self._batch

    def commit_attachment(self) -> None:
        self.committed = True

    def discard_attachment(self) -> None:
        self.discarded = True

    def revoke_attachment(self) -> None:
        self.revoked = True

    def admit_exact_location(self, *, location: str, session_id: str) -> str:
        self.admitted = (location, session_id)
        return "accepted"

    async def history(self, *, last_n: int) -> History:
        return History(location="/work/t.jsonl")

    async def history_page(
        self,
        *,
        before: str | None = None,
        snapshot: str | None = None,
        limit: int = 100,
        include_full_text: bool = False,
    ) -> HistoryPage:
        self.history_page_call = (before, snapshot, limit, include_full_text)
        return HistoryPage(location="/work/t.jsonl")


class _PrimaryWithIdentityLoss(Source):
    async def read(self) -> Batch:
        return Batch()

    async def probe_identity_loss(self) -> IdentityLossEvidence | None:
        return IdentityLossEvidence(location="/work/other.jsonl", session_id="other")


@pytest.mark.asyncio
async def test_no_primary_still_reads_enrichments() -> None:
    fact_a = make_fact("a", 0)
    fact_b = make_fact("b", 0)
    enrich_a = ControlledSource()
    enrich_a.set_batch(Batch(trajectory=[fact_a]))
    enrich_b = ControlledSource()
    enrich_b.set_batch(Batch(trajectory=[fact_b]))
    composite = CompositeSource(
        enrichments=[
            EnrichmentBinding(source=enrich_a, declaration=decl("a")),
            EnrichmentBinding(source=enrich_b, declaration=decl("b")),
        ],
    )
    batch = await composite.read()
    assert batch.progressed is False
    assert batch.status is None
    assert len(batch.trajectory) == 2
    assert batch.trajectory[0].native_id == "a"
    assert batch.trajectory[1].native_id == "b"
    await composite.aclose()


@pytest.mark.asyncio
async def test_no_primary_read_with_no_enrichments_returns_empty() -> None:
    composite = CompositeSource()
    batch = await composite.read()
    assert batch == Batch()
    await composite.aclose()


@pytest.mark.asyncio
async def test_no_primary_delegates_to_source_defaults() -> None:
    composite = CompositeSource()
    batch = await composite.read()
    assert batch.trajectory == ()
    refresh = await composite.refresh()
    assert refresh == Batch()
    identity = await composite.probe_identity_loss()
    assert identity is None
    history = await composite.history(last_n=10)
    assert history == History()
    page = await composite.history_page()
    assert page == HistoryPage()


@pytest.mark.parametrize(
    "operation",
    [
        lambda c: c.commit_attachment(),
        lambda c: c.discard_attachment(),
        lambda c: c.revoke_attachment(),
        lambda c: c.admit_exact_location(location="x", session_id="s"),
    ],
    ids=["commit", "discard", "revoke", "admit_exact_location"],
)
def test_no_primary_refuses_attachment_operations(operation) -> None:
    with pytest.raises(SourceContractError):
        operation(CompositeSource())


@pytest.mark.asyncio
async def test_no_primary_refuses_history_paging() -> None:
    composite = CompositeSource()
    page = await composite.history_page(before="cursor")
    assert page.error_code == "history_paging_unavailable"


@pytest.mark.asyncio
async def test_no_primary_collision_domain_none() -> None:
    composite = CompositeSource()
    assert composite.collision_domain is None


@pytest.mark.parametrize(
    ("method", "flag", "read_first"),
    [
        ("commit_attachment", "committed", True),
        ("discard_attachment", "discarded", False),
        ("revoke_attachment", "revoked", False),
    ],
    ids=["commit", "discard", "revoke"],
)
@pytest.mark.asyncio
async def test_attachment_calls_delegate_to_primary(method, flag, read_first) -> None:
    primary = _PrimaryWithAttachment()
    composite = CompositeSource(primary=primary)
    if read_first:
        assert (await composite.read()).attached is not None
    getattr(composite, method)()
    assert getattr(primary, flag) is True
    await composite.aclose()


@pytest.mark.asyncio
async def test_admit_exact_location_delegates_to_primary() -> None:
    primary = _PrimaryWithAttachment()
    composite = CompositeSource(primary=primary)
    result = composite.admit_exact_location(location="/work/t.jsonl", session_id="s1")
    assert result == "accepted"
    assert primary.admitted == ("/work/t.jsonl", "s1")
    await composite.aclose()


@pytest.mark.asyncio
async def test_history_delegates_to_primary() -> None:
    primary = _PrimaryWithAttachment()
    composite = CompositeSource(primary=primary)
    history = await composite.history(last_n=10)
    assert history.location == "/work/t.jsonl"
    await composite.aclose()


@pytest.mark.asyncio
async def test_history_page_delegates_to_primary() -> None:
    primary = _PrimaryWithAttachment()
    composite = CompositeSource(primary=primary)
    page = await composite.history_page(snapshot="snapshot", limit=10, include_full_text=True)
    assert page.location == "/work/t.jsonl"
    assert primary.history_page_call == (None, "snapshot", 10, True)
    await composite.aclose()


@pytest.mark.asyncio
async def test_probe_identity_loss_delegates_to_primary() -> None:
    primary = _PrimaryWithIdentityLoss()
    composite = CompositeSource(primary=primary)
    evidence = await composite.probe_identity_loss()
    assert evidence is not None
    assert evidence.location == "/work/other.jsonl"
    await composite.aclose()


@pytest.mark.asyncio
async def test_collision_domain_from_primary() -> None:
    primary = _PrimaryWithAttachment()
    composite = CompositeSource(primary=primary)
    assert composite.collision_domain == "domain"
    await composite.aclose()


@pytest.mark.asyncio
async def test_collision_domain_reflects_primary_updates() -> None:
    class _MutableDomain(Source):
        collision_domain: str | None = None

        async def read(self) -> Batch:
            return Batch()

    primary = _MutableDomain()
    composite = CompositeSource(primary=primary)
    assert composite.collision_domain is None
    primary.collision_domain = "updated"
    assert composite.collision_domain == "updated"
    await composite.aclose()


def test_bind_wakeup_reaches_the_primary_and_not_enrichments() -> None:
    class _Bindable(RecordingSource):
        def __init__(self) -> None:
            super().__init__()
            self.bound: list[object] = []

        def bind_wakeup(self, wakeup) -> None:
            self.bound.append(wakeup)

    primary, enrichment = _Bindable(), _Bindable()
    composite = CompositeSource(
        primary=primary,
        enrichments=[EnrichmentBinding(source=enrichment, declaration=decl("hook"))],
    )
    wake = object()
    composite.bind_wakeup(wake)  # type: ignore[arg-type]
    assert primary.bound == [wake]
    assert enrichment.bound == []
    CompositeSource().bind_wakeup(wake)  # type: ignore[arg-type]  # no primary: a no-op
