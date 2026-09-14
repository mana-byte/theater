"""Labelled accumulate-and-fail runner for batched parametrize tables.

A plain assert-loop hides every failure after the first violated row; batching
a parametrize family into one collected item keeps all rows visible in the
failure by collecting labelled exceptions and asserting once at the end.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable

Row = tuple[str, Callable[[], object]]
AsyncRow = tuple[str, Callable[[], Awaitable[object]]]


def eq_row(label: str, fn: Callable[[], object], want: object) -> Row:
    """Lazy equality row: fn() runs inside the runner, so failures stay labelled."""

    def check() -> None:
        got = fn()
        assert got == want, f"{got!r} != {want!r}"

    return label, check


def is_row(label: str, fn: Callable[[], object], want: object) -> Row:
    """Lazy identity row for None and enum comparisons."""

    def check() -> None:
        got = fn()
        assert got is want, f"{got!r} is not {want!r}"

    return label, check


def run_rows(rows: Iterable[Row]) -> None:
    """Run every labelled row, then assert once, reporting every failure.

    Expected-exception rows keep pytest.raises semantics inside the row: the
    wrong exception type fails the row. Interrupts and cancellation are never
    caught (``Exception`` only), and nothing is ever swallowed silently — every
    escaping exception is a labelled failure in the final assertion.
    """
    rows = list(rows)
    failures: list[str] = []
    for label, row in rows:
        try:
            row()
        except Exception as exc:
            failures.append(f"{label}: {exc!r}")
    assert not failures, f"{len(failures)} of {len(rows)} rows failed:\n" + "\n".join(failures)


async def run_rows_async(rows: Iterable[AsyncRow]) -> None:
    """Await every labelled async row, then assert once (same semantics as run_rows)."""
    rows = list(rows)
    failures: list[str] = []
    for label, row in rows:
        try:
            await row()
        except Exception as exc:
            failures.append(f"{label}: {exc!r}")
    assert not failures, f"{len(failures)} of {len(rows)} rows failed:\n" + "\n".join(failures)
