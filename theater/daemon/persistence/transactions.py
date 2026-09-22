"""Contracts for short daemon-owned transactional write units."""

from __future__ import annotations

import logging
from collections.abc import Callable
from types import TracebackType
from typing import Literal, Protocol, Self, cast

from sqlalchemy import Connection, Engine
from sqlalchemy.engine import Transaction

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


class SQLiteWriteUnit:
    """Concrete short write unit; callers perform only synchronous DB work."""

    def __init__(
        self,
        engine: Engine,
        *,
        enter: Callable[[], None],
        leave: Callable[[], None],
    ) -> None:
        self._engine = engine
        self._enter = enter
        self._leave = leave
        self._connection: Connection | None = None
        self._transaction: Transaction | None = None
        self._notifications: list[AfterCommit] = []
        self._used = False

    @property
    def connection(self) -> Connection:
        if self._connection is None:
            raise RuntimeError("write unit is not active")
        return self._connection

    def after_commit(self, notification: AfterCommit) -> None:
        if self._connection is None:
            raise RuntimeError("after-commit notifications require an active write unit")
        self._notifications.append(notification)

    def __enter__(self) -> Self:
        if self._used:
            raise RuntimeError("write unit cannot be reused")
        self._enter()
        self._used = True
        try:
            self._connection = self._engine.connect()
            self._transaction = self._connection.begin()
        except BaseException:
            if self._connection is not None:
                self._connection.close()
                self._connection = None
            self._leave()
            raise
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        transaction = cast(Transaction, self._transaction)
        connection = cast(Connection, self._connection)
        committed = False
        try:
            if exception_type is None:
                transaction.commit()
                committed = True
            else:
                transaction.rollback()
        finally:
            connection.close()
            self._connection = None
            self._transaction = None
            self._leave()
        if committed:
            for notification in self._notifications:
                try:
                    notification()
                except Exception:
                    logging.getLogger("theater.persistence").exception(
                        "after-commit notification failed"
                    )
        return False


__all__ = ["AfterCommit", "SQLiteWriteUnit", "WriteUnit", "WriteUnitFactory"]
