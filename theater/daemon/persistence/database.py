"""Database owner: engine, pragmas, migrations, connections, close."""

from __future__ import annotations

import errno
import fcntl
import os
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from alembic.script.revision import ResolutionError
from sqlalchemy import Connection, create_engine, event, inspect, text
from sqlalchemy.engine import Engine

from theater import paths
from theater.daemon.persistence.transactions import SQLiteWriteUnit

MIGRATIONS = Path(__file__).parent.parent / "migrations"

#: The revision a pre-Alembic database is already at. See ``_stamp_legacy``.
BASELINE = "0001"

#: The latest revision. A legacy DB is stamped at BASELINE then upgraded here.
HEAD = "0034"

#: Crossing this revision permanently ends the one-time RC9 drain requirement.
RC10_BOUNDARY = "0032"

_BLOCKER_LIMIT = 20


class RC9UpgradeBlocked(RuntimeError):
    """The RC9 database still owns live work and cannot cross into RC10."""

    def __init__(self, participants: list[str], jobs: list[str], counts: tuple[int, int]):
        self.participant_ids = tuple(participants)
        self.job_handles = tuple(jobs)
        self.participant_count, self.job_count = counts
        super().__init__(
            "RC10 migration requires a drained RC9 database; stop all participants and "
            "finish all running jobs before retrying. "
            f"Blocking participants ({self.participant_count}): {participants}; "
            f"running jobs ({self.job_count}): {jobs}"
        )


class RC9UpgradeLockHeld(RuntimeError):
    """A running daemon or another migration owns the database upgrade lock."""

    def __init__(self, path: Path) -> None:
        super().__init__(
            f"cannot upgrade {path}: another daemon or upgrade holds the database lock"
        )


class _UpgradeLock:
    """A shared daemon / exclusive migration lock beside the database."""

    def __init__(self, path: Path, *, exclusive: bool) -> None:
        self.path = path
        self.exclusive = exclusive
        self._fd = self._open(path.with_name(f"{path.name}.upgrade.lock"))
        try:
            fcntl.flock(
                self._fd,
                (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB,
            )
        except OSError as exc:
            os.close(self._fd)
            if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK}:
                raise RC9UpgradeLockHeld(path) from exc
            raise

    @staticmethod
    def _open(path: Path) -> int:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            return os.open(path, flags)
        except FileNotFoundError:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            return os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)

    def downgrade_to_shared(self) -> None:
        if self.exclusive:
            fcntl.flock(self._fd, fcntl.LOCK_SH)
            self.exclusive = False

    def close(self) -> None:
        fd, self._fd = self._fd, -1
        if fd >= 0:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)


class _DaemonCompatibilityUpgradeLock:
    """Take RC9's daemon flock without changing its diagnostic contents."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self.path = paths.pidfile_path()
        self._created = False
        self._fd = self._open()
        try:
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(self._fd)
            self._fd = -1
            if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK}:
                raise RC9UpgradeLockHeld(database_path) from exc
            raise

    def _open(self) -> int:
        flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        try:
            return os.open(self.path, flags)
        except FileNotFoundError:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            try:
                fd = os.open(self.path, flags | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                return os.open(self.path, flags)
            self._created = True
            return fd

    def close(self) -> None:
        fd, self._fd = self._fd, -1
        if fd < 0:
            return
        if self._created:
            try:
                current = self.path.stat()
                opened = os.fstat(fd)
            except OSError:
                pass
            else:
                if (current.st_dev, current.st_ino) == (opened.st_dev, opened.st_ino):
                    self.path.unlink(missing_ok=True)
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


@contextmanager
def exclusive_upgrade_lock(path: Path) -> Iterator[None]:
    """Exclude RC9/RC10 daemons and migrations from preflight through DDL."""
    daemon_lock = _DaemonCompatibilityUpgradeLock(path)
    lock: _UpgradeLock | None = None
    try:
        lock = _UpgradeLock(path, exclusive=True)
        yield
    finally:
        if lock is not None:
            lock.close()
        daemon_lock.close()


def revision_is_rc10(revision: str | None) -> bool:
    """Whether the revision's Alembic ancestry includes the RC10 boundary."""
    if revision is None:
        return False
    try:
        ancestry = ScriptDirectory(str(MIGRATIONS)).iterate_revisions(revision, "base")
        return any(item.revision == RC10_BOUNDARY for item in ancestry)
    except ResolutionError:
        return False


def _raise_if_blocked(connection) -> None:
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    if "participants" not in tables:
        return
    if "alembic_version" in tables:
        revisions = connection.execute("SELECT version_num FROM alembic_version").fetchall()
        if any(revision_is_rc10(row[0]) for row in revisions):
            return
    participant_count = connection.execute(
        "SELECT count(*) FROM participants WHERE status != 'dead'"
    ).fetchone()[0]
    participant_ids = [
        row[0]
        for row in connection.execute(
            "SELECT id FROM participants WHERE status != 'dead' ORDER BY id LIMIT ?",
            (_BLOCKER_LIMIT,),
        ).fetchall()
    ]
    job_count = 0
    job_handles: list[str] = []
    if "jobs" in tables:
        job_count = connection.execute(
            "SELECT count(*) FROM jobs WHERE state = 'running'"
        ).fetchone()[0]
        job_handles = [
            row[0]
            for row in connection.execute(
                "SELECT handle FROM jobs WHERE state = 'running' ORDER BY handle LIMIT ?",
                (_BLOCKER_LIMIT,),
            ).fetchall()
        ]
    if participant_count or job_count:
        raise RC9UpgradeBlocked(
            participant_ids,
            job_handles,
            (int(participant_count), int(job_count)),
        )


def preflight_rc9_upgrade_path(path: Path) -> None:
    """Read an existing file without pragmas or writes before engine startup."""
    if not path.exists() or path.stat().st_size == 0:
        return
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        _raise_if_blocked(connection)
    finally:
        connection.close()


def ensure_rc9_upgrade_allowed(connection: Connection) -> None:
    """Apply the same drain guard to an online Alembic connection."""
    tables = set(inspect(connection).get_table_names())
    if "participants" not in tables:
        return
    if "alembic_version" in tables:
        revisions = connection.execute(text("SELECT version_num FROM alembic_version")).scalars()
        if any(revision_is_rc10(revision) for revision in revisions):
            return
    participant_count = int(
        connection.execute(
            text("SELECT count(*) FROM participants WHERE status != 'dead'")
        ).scalar_one()
    )
    participant_ids = list(
        connection.execute(
            text("SELECT id FROM participants WHERE status != 'dead' ORDER BY id LIMIT :limit"),
            {"limit": _BLOCKER_LIMIT},
        ).scalars()
    )
    job_count = 0
    job_handles: list[str] = []
    if "jobs" in tables:
        job_count = int(
            connection.execute(
                text("SELECT count(*) FROM jobs WHERE state = 'running'")
            ).scalar_one()
        )
        job_handles = list(
            connection.execute(
                text(
                    "SELECT handle FROM jobs WHERE state = 'running' ORDER BY handle LIMIT :limit"
                ),
                {"limit": _BLOCKER_LIMIT},
            ).scalars()
        )
    if participant_count or job_count:
        raise RC9UpgradeBlocked(
            participant_ids,
            job_handles,
            (participant_count, job_count),
        )


def _set_pragmas(dbapi_connection, _record) -> None:
    """WAL, foreign keys, and busy_timeout for every connection."""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


class Database:
    """Owns the engine, the long-lived autocommit connection, and migrations."""

    def __init__(self, path: Path):
        self.path = path
        self._upgrade_lock = _UpgradeLock(path, exclusive=True)
        self._owner_thread = threading.get_ident()
        self._write_unit_active = False
        self.engine: Engine
        engine: Engine | None = None
        try:
            # Do not chmod, stamp, or even configure SQLite before a live RC9
            # database has declined the guarded upgrade while our lock is held.
            preflight_rc9_upgrade_path(path)
            paths.ensure_private_file(path)
            engine = create_engine(f"sqlite:///{path}")
            self.engine = engine
            event.listen(engine, "connect", _set_pragmas)

            with engine.connect() as conn:
                self._stamp_legacy(conn)
                self._upgrade(conn)
                conn.commit()

            self._upgrade_lock.downgrade_to_shared()
            # Long-lived autocommit: callers never commit, writes visible immediately.
            self.conn = engine.connect().execution_options(isolation_level="AUTOCOMMIT")
        except BaseException:
            if engine is not None:
                engine.dispose()
            self._upgrade_lock.close()
            raise

    def _enter_write_unit(self) -> None:
        if threading.get_ident() != self._owner_thread:
            raise RuntimeError("write units must run on the database owner thread")
        if self._write_unit_active:
            raise RuntimeError("nested write units are not allowed")
        self._write_unit_active = True

    def _leave_write_unit(self) -> None:
        self._write_unit_active = False

    def write_unit(self) -> SQLiteWriteUnit:
        """Return one short synchronous transaction for cooperating repositories."""
        return SQLiteWriteUnit(
            self.engine,
            enter=self._enter_write_unit,
            leave=self._leave_write_unit,
        )

    # ---- migrations ----------------------------------------------------

    def _config(self, conn: Connection) -> Config:
        """An in-memory Alembic config bound to an existing connection."""
        cfg = Config()
        cfg.set_main_option("script_location", str(MIGRATIONS))
        cfg.attributes["connection"] = conn
        cfg.attributes["rc9_upgrade_lock_held"] = True
        return cfg

    def _stamp_legacy(self, conn: Connection) -> None:
        """Adopt a pre-1.3 database instead of rebuilding it."""
        tables = set(inspect(conn).get_table_names())
        if "participants" not in tables or "alembic_version" in tables:
            return
        command.stamp(self._config(conn), BASELINE)

    def _upgrade(self, conn: Connection) -> None:
        command.upgrade(self._config(conn), "head")

    def close(self) -> None:
        self.conn.close()
        self.engine.dispose()
        self._upgrade_lock.close()


__all__ = [
    "BASELINE",
    "HEAD",
    "MIGRATIONS",
    "RC10_BOUNDARY",
    "Database",
    "RC9UpgradeBlocked",
    "RC9UpgradeLockHeld",
    "ensure_rc9_upgrade_allowed",
    "exclusive_upgrade_lock",
    "preflight_rc9_upgrade_path",
    "revision_is_rc10",
]
