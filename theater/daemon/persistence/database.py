"""Database owner: engine, pragmas, migrations, connections, close."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from alembic.script.revision import ResolutionError
from sqlalchemy import Connection, create_engine, event, inspect, text

from theater import paths
from theater.daemon.persistence.transactions import SQLiteWriteUnit

MIGRATIONS = Path(__file__).parent.parent / "migrations"

#: The revision a pre-Alembic database is already at. See ``_stamp_legacy``.
BASELINE = "0001"

#: The latest revision. A legacy DB is stamped at BASELINE then upgraded here.
HEAD = "0032"

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
        paths.ensure_private_file(path)
        preflight_rc9_upgrade_path(path)
        self._owner_thread = threading.get_ident()
        self._write_unit_active = False
        self.engine = create_engine(f"sqlite:///{path}")
        event.listen(self.engine, "connect", _set_pragmas)

        with self.engine.connect() as conn:
            self._stamp_legacy(conn)
            self._upgrade(conn)
            conn.commit()

        # Long-lived autocommit: callers never commit, writes visible immediately.
        self.conn = self.engine.connect().execution_options(isolation_level="AUTOCOMMIT")

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


__all__ = [
    "BASELINE",
    "HEAD",
    "MIGRATIONS",
    "RC10_BOUNDARY",
    "Database",
    "RC9UpgradeBlocked",
    "ensure_rc9_upgrade_allowed",
    "preflight_rc9_upgrade_path",
    "revision_is_rc10",
]
