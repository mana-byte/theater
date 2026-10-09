"""Contracts for short daemon-owned transactional write units."""

from __future__ import annotations

import logging
from collections.abc import Callable
from types import TracebackType
from typing import Literal, Protocol, Self, cast

from sqlalchemy import Connection, Engine
from sqlalchemy.engine import Transaction

AfterCommit = Callable[[], None]
_UNIT_KEY = "theater.persistence.write_unit"


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

    def __call__(self, *, connection: Connection | None = None) -> WriteUnit: ...


def _dbapi_in_transaction(connection: Connection) -> bool:
    """Whether the driver holds an open transaction, ignoring SQLAlchemy autobegin bookkeeping."""
    state = getattr(connection.connection.driver_connection, "in_transaction", None)
    return connection.in_transaction() if state is None else bool(state)


def after_commit(connection: Connection | None, notification: AfterCommit) -> None:
    """Run ``notification`` after the active unit commits; immediately only on autocommit."""
    unit = None if connection is None else connection.info.get(_UNIT_KEY)
    if unit is not None:
        unit.after_commit(notification)
    elif connection is not None and _dbapi_in_transaction(connection):
        raise RuntimeError(
            "after-commit notification requested inside an external transaction that no write "
            "unit owns; wrap the writes in store.write_unit(connection=...) so listeners fire "
            "only after the real commit, or use an autocommit connection"
        )
    else:
        notification()


def active_write_unit(connection: Connection | None) -> WriteUnit | None:
    """Return the write unit currently owning ``connection``, if any."""
    return None if connection is None else connection.info.get(_UNIT_KEY)


class SQLiteWriteUnit:
    """Concrete short write unit; callers perform only synchronous DB work."""

    def __init__(
        self,
        engine: Engine,
        *,
        enter: Callable[[], None],
        leave: Callable[[], None],
        connection: Connection | None = None,
    ) -> None:
        self._engine = engine
        self._enter = enter
        self._leave = leave
        self._provided_connection = connection
        self._owns_connection = connection is None
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

    @staticmethod
    def _require_idle(connection: Connection) -> None:
        if connection.get_execution_options().get("isolation_level") == "AUTOCOMMIT":
            # Reads autobegin SQLAlchemy bookkeeping; only an open driver transaction is real.
            busy = _dbapi_in_transaction(connection)
        else:
            busy = connection.in_transaction()
        if busy:
            raise RuntimeError("write unit connection already has an active transaction")

    def __enter__(self) -> Self:
        if self._used:
            raise RuntimeError("write unit cannot be reused")
        self._enter()
        self._used = True
        try:
            self._connection = self._provided_connection or self._engine.connect()
            self._require_idle(self._connection)
            if self._connection.get_execution_options().get("isolation_level") == "AUTOCOMMIT":
                self._connection.exec_driver_sql("BEGIN")
            else:
                self._transaction = self._connection.begin()
            self._connection.info[_UNIT_KEY] = self
        except BaseException:
            if self._connection is not None and self._owns_connection:
                self._connection.close()
            self._connection = None
            self._leave()
            raise
        return self

    def __exit__(  # noqa: PLR0912
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        transaction = self._transaction
        connection = cast(Connection, self._connection)
        committed = False
        try:
            if exception_type is None:
                if transaction is None:
                    connection.commit()
                else:
                    transaction.commit()
                committed = True
            elif transaction is None:
                connection.rollback()
            else:
                transaction.rollback()
        except BaseException:
            if exception_type is None:
                connection.rollback()
            raise
        finally:
            if connection.info.get(_UNIT_KEY) is self:
                connection.info.pop(_UNIT_KEY, None)
            if self._owns_connection:
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


__all__ = [
    "AfterCommit",
    "SQLiteWriteUnit",
    "WriteUnit",
    "WriteUnitFactory",
    "active_write_unit",
    "after_commit",
]
