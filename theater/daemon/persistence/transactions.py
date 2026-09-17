"""Contracts for short daemon-owned transactional write units."""

from __future__ import annotations

from collections.abc import Callable
from types import TracebackType
from typing import Protocol, Self

from sqlalchemy import Connection

AfterCommit = Callable[[], None]


class WriteUnit(Protocol):
    """One synchronous SQLite transaction with post-commit notifications.

    All writers share ``connection``. Awaiting and external I/O are forbidden;
    callbacks run only after a successful commit.
    """

    @property
    def connection(self) -> Connection: ...

    def after_commit(self, notification: AfterCommit) -> None: ...

    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None: ...


class WriteUnitFactory(Protocol):
    """Factory boundary supplied by Wave 02's daemon persistence implementation."""

    def __call__(self) -> WriteUnit: ...


__all__ = ["AfterCommit", "WriteUnit", "WriteUnitFactory"]
