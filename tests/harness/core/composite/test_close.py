"""Closing a composite source attempts every child once and preserves cancellation."""

from __future__ import annotations

import asyncio

import pytest

from tests.support.composite_sources import RecordingSource, decl
from theater.harness.channels import CompositeSource, EnrichmentBinding
from theater.harness.contracts.source import Batch, Source


class _CancelCloseSource(Source):
    async def read(self) -> Batch:
        return Batch()

    async def aclose(self) -> None:
        raise asyncio.CancelledError


@pytest.mark.asyncio
async def test_aclose_is_idempotent() -> None:
    source = RecordingSource()
    composite = CompositeSource(enrichments=[EnrichmentBinding(source=source, declaration=decl())])
    await composite.aclose()
    await composite.aclose()
    assert source.close_count == 1


@pytest.mark.asyncio
async def test_aclose_attempts_every_source_exactly_once() -> None:
    s1 = RecordingSource()
    s2 = RecordingSource()
    composite = CompositeSource(
        enrichments=[
            EnrichmentBinding(source=s1, declaration=decl("a")),
            EnrichmentBinding(source=s2, declaration=decl("b")),
        ]
    )
    await composite.aclose()
    assert s1.close_count == 1
    assert s2.close_count == 1


@pytest.mark.asyncio
async def test_aclose_raises_first_real_error_in_declaration_order() -> None:
    class CloseErrorSource(Source):
        def __init__(self, msg: str) -> None:
            self._msg = msg

        async def read(self) -> Batch:
            return Batch()

        async def aclose(self) -> None:
            raise RuntimeError(self._msg)

    err1 = CloseErrorSource("err1")
    err2 = CloseErrorSource("err2")
    composite = CompositeSource(
        enrichments=[
            EnrichmentBinding(source=err1, declaration=decl("first")),
            EnrichmentBinding(source=err2, declaration=decl("second")),
        ]
    )
    with pytest.raises(RuntimeError, match="err1"):
        await composite.aclose()


@pytest.mark.asyncio
async def test_aclose_preserves_cancellation() -> None:
    class CancelMeSource(Source):
        async def read(self) -> Batch:
            return Batch()

        async def aclose(self) -> None:
            await asyncio.sleep(10.0)

    composite = CompositeSource(
        enrichments=[EnrichmentBinding(source=CancelMeSource(), declaration=decl())]
    )
    close_task = asyncio.create_task(composite.aclose())
    await asyncio.sleep(0.05)
    close_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await close_task


@pytest.mark.asyncio
async def test_aclose_propagates_child_cancelled_error() -> None:
    composite = CompositeSource(
        enrichments=[EnrichmentBinding(source=_CancelCloseSource(), declaration=decl("c"))]
    )
    with pytest.raises(asyncio.CancelledError):
        await composite.aclose()
