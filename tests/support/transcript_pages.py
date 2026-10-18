"""An in-memory history source and the pager and page builders around it."""

from __future__ import annotations

from theater.daemon.rpc.transcript_paging import TranscriptPager
from theater.harness import Event
from theater.harness.contracts.source import Batch, HistoryPage, Source


class StaticSource(Source):
    def __init__(self, pages: dict[str | None, HistoryPage]) -> None:
        self.pages = pages
        self.calls: list[tuple[str | None, str | None, bool]] = []

    async def read(self) -> Batch:
        return Batch()

    async def history_page(
        self,
        *,
        before: str | None = None,
        snapshot: str | None = None,
        limit: int = 200,
        include_full_text: bool = False,
    ) -> HistoryPage:
        self.calls.append((before, snapshot, include_full_text))
        if snapshot is not None:
            return next(
                (page for page in self.pages.values() if page.snapshot_cursor == snapshot),
                self.pages[before],
            )
        return self.pages[before]


def page(
    events: tuple[Event, ...],
    *,
    cursor: str,
    snapshot_cursor: str | None = None,
    older_cursor: str | None = None,
    location: str = "/opaque/transcript",
) -> HistoryPage:
    return HistoryPage(
        location=location,
        events=events,
        complete_events=events,
        cursor=cursor,
        snapshot_cursor=snapshot_cursor or cursor,
        older_cursor=older_cursor,
        has_older=older_cursor is not None,
        provenance="operator",
    )


def pager(source: Source, target: str = "p1") -> TranscriptPager:
    return TranscriptPager(source, target=target, event_filter=lambda _event: True)
