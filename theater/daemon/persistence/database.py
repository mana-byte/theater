"""Database owner: engine, pragmas, migrations, connections, close.

Synchronous and daemon-only — the daemon is the sole SQLite writer.
One long-lived autocommit connection for routine writes; fresh transactional
connections opened via ``engine.begin()`` for atomic cross-table operations.

Schema changes go through Alembic (``migrations/versions/``), never here.
"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, create_engine, event, inspect

MIGRATIONS = Path(__file__).parent.parent / "migrations"

#: The revision a pre-Alembic database is already at. See ``_stamp_legacy``.
BASELINE = "0001"

#: The latest revision. A legacy DB is stamped at BASELINE then upgraded here.
HEAD = "0020"


def _set_pragmas(dbapi_connection, _record) -> None:
    """WAL, foreign keys, and busy_timeout for every connection.

    WAL so a reader never blocks the daemon's writes; foreign keys because
    SQLite disables them per connection; busy_timeout so a writer that cannot
    acquire the lock waits up to 5s rather than failing instantly.
    """
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


class Database:
    """Owns the engine, the long-lived autocommit connection, and migrations.

    Repositories receive a ``Database`` and execute against ``db.conn`` or
    ``db.engine.begin()`` — preserving the exact transaction boundaries of
    the original ``Store``.
    """

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(f"sqlite:///{path}")
        event.listen(self.engine, "connect", _set_pragmas)

        with self.engine.connect() as conn:
            self._stamp_legacy(conn)
            self._upgrade(conn)
            conn.commit()

        # Long-lived autocommit: callers never commit, writes visible immediately.
        self.conn = self.engine.connect().execution_options(isolation_level="AUTOCOMMIT")

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
