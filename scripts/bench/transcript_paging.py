"""Measure read_transcript paging cost on synthetic transcripts (WP-C6).

Runs the daemon's TranscriptPager against in-memory histories; no network, no files.
Run it with this checkout for "after" and with a base checkout (uv run --project <base>)
for "before": a pager without roles/collapse is detected and read the old way.
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json

from theater.daemon.rpc.transcript_paging import TranscriptPager
from theater.harness import Event, EventKind
from theater.harness.contracts.source import Batch, HistoryPage, Source

KIB = 1024
MIB = 1024 * KIB
COLLAPSE_OVER = 64 * KIB


class StaticSource(Source):
    """One in-memory history page, the approach tests/transcripts/test_paging.py uses."""

    def __init__(self, events: tuple[Event, ...]) -> None:
        self.page = HistoryPage(
            location="/bench/transcript",
            events=events,
            complete_events=events,
            cursor="newest",
            snapshot_cursor="newest",
            older_cursor=None,
            has_older=False,
            provenance="operator",
        )

    async def read(self) -> Batch:
        return Batch()

    async def history_page(self, **_kwargs: object) -> HistoryPage:
        return self.page


def modern() -> bool:
    return "roles" in inspect.signature(TranscriptPager.__init__).parameters


def make_pager(source: Source, roles: tuple[str, ...] | None = None) -> TranscriptPager:
    kwargs: dict[str, object] = {}
    if modern():
        kwargs = {"roles": roles, "collapse_over": COLLAPSE_OVER}
    return TranscriptPager(source, target="p1", event_filter=lambda _event: True, **kwargs)


def wire_bytes(page) -> int:
    payload = page.to_wire(target="p1")
    return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode())


def text(size: int, seed: int = 0) -> str:
    return "".join(chr(0x61 + (seed + index) % 26) for index in range(size))


def events(*specs: tuple[EventKind, str]) -> tuple[Event, ...]:
    return tuple(
        Event(kind=kind, text=body, raw_index=index) for index, (kind, body) in enumerate(specs)
    )


async def calls_until(
    source: Source, found, roles: tuple[str, ...] | None = None
) -> tuple[int, int]:
    """Calls (and bytes of the final call) until a page satisfies `found`."""
    pager = make_pager(source, roles)
    page = await pager.read()
    calls = 1
    while not found(page):
        if page.next_cursor is None:
            raise SystemExit("history exhausted before the target event was seen")
        page = await pager.read(page.next_cursor)
        calls += 1
    return calls, wire_bytes(page)


def has_index(index: int, *, whole: bool = False):
    def check(page) -> bool:
        return any(
            chunk.event.raw_index == index and (not whole or chunk.reaches_text_start)
            for chunk in page.events
        )

    return check


async def big_event_rows() -> list[tuple[str, str, str]]:
    rows = []
    for label, size in (("100 KiB", 100 * KIB), ("1.4 MB", 1_400_000), ("10 MB", 10 * MIB)):
        source = StaticSource(
            events(
                (EventKind.USER, "first message"),
                (EventKind.ASSISTANT, "older answer"),
                (EventKind.TOOL_RESULT, text(size)),
                (EventKind.ASSISTANT, "newest answer"),
            )
        )
        calls, _ = await calls_until(source, has_index(1))
        rows.append((f"read past a {label} tool_result", "calls", str(calls)))
    return rows


def mixed_history() -> StaticSource:
    specs: list[tuple[EventKind, str]] = [(EventKind.USER, "first message: " + text(400))]
    for turn in range(20):
        specs += [
            (EventKind.ASSISTANT, text(500, turn)),
            (EventKind.TOOL_CALL, text(300, turn)),
            (EventKind.TOOL_RESULT, text(3 * KIB if turn != 10 else 1_400_000, turn)),
        ]
    specs.append((EventKind.ASSISTANT, "final answer: " + text(1500)))
    return StaticSource(events(*specs))


async def answer_rows() -> list[tuple[str, str, str]]:
    source = mixed_history()
    last = len(source.page.events) - 1
    user_roles = ("user",) if modern() else None
    first_calls, _ = await calls_until(source, has_index(0, whole=True), user_roles)
    answer_roles = ("assistant",) if modern() else None
    answer_calls, answer_bytes = await calls_until(source, has_index(last), answer_roles)
    first_page = await make_pager(source, answer_roles).read()
    answers = sum(chunk.event.kind is EventKind.ASSISTANT for chunk in first_page.events)
    return [
        ("first message (roles=['user'] after)", "calls", str(first_calls)),
        ("last answer (roles=['assistant'] after)", "calls", str(answer_calls)),
        ("last answer (roles=['assistant'] after)", "bytes returned", str(answer_bytes)),
        ("assistant events in that one call", "events", str(answers)),
    ]


async def small_event_row() -> tuple[str, str, str]:
    source = StaticSource(events((EventKind.USER, "ask"), (EventKind.TOOL_RESULT, text(10 * KIB))))
    page = await make_pager(source).read()
    chunk = next(chunk for chunk in page.events if chunk.event.raw_index == 1)
    whole = chunk.start == 0 and chunk.end == chunk.total and chunk.text == text(10 * KIB)
    return ("10 KiB tool_result on first page", "returned whole", "yes" if whole else "no")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    rows = [*await big_event_rows(), *await answer_rows(), await small_event_row()]
    print(f"mode={'after (roles+collapse)' if modern() else 'before (byte-split)'}")
    for item, metric, value in rows:
        print(f"  {item:<44} {metric:<16} {value}")


if __name__ == "__main__":
    asyncio.run(main())
