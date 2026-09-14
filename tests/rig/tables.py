"""Labelled accumulate-and-fail runner for batched parametrize tables.

A plain assert-loop hides every failure after the first violated row; batching
a parametrize family into one collected item keeps all rows visible in the
failure by collecting labelled exceptions and asserting once at the end.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

Row = tuple[str, Callable[[], object]]


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
    assert not failures, (
        f"{len(failures)} of {len(rows)} rows failed:\n" + "\n".join(failures)
    )
