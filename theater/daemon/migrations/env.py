"""Alembic environment for the daemon (connection via ``config.attributes``) and dev autogenerate.

``render_as_batch`` matters: SQLite lacks real ALTER TABLE, so batch mode copies and swaps tables.
"""

from __future__ import annotations

from pathlib import Path

from alembic import context
from sqlalchemy import create_engine

from theater import paths
from theater.daemon.persistence.database import (
    ensure_rc9_upgrade_allowed,
    exclusive_upgrade_lock,
    preflight_rc9_upgrade_path,
)
from theater.daemon.schema import metadata

target_metadata = metadata


def _url() -> str:
    return f"sqlite:///{paths.db_path()}"


def _configure(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=True,
        compare_type=True,
    )


def run_migrations_offline() -> None:
    raise RuntimeError(
        "offline Alembic migration is unsupported because the RC9 drain guard "
        "must inspect the live database before RC10 schema changes"
    )


def run_migrations_online() -> None:
    connection = context.config.attributes.get("connection")
    if connection is not None:
        if context.config.attributes.get("rc9_upgrade_lock_held"):
            _run_with_connection(connection)
            return
        path = _connection_path(connection)
        with exclusive_upgrade_lock(path):
            _run_with_connection(connection)
        return

    path = paths.db_path()
    with exclusive_upgrade_lock(path):
        preflight_rc9_upgrade_path(path)
        paths.ensure_home()
        paths.ensure_private_file(path)
        engine = create_engine(_url())
        try:
            with engine.connect() as conn:
                _run_with_connection(conn)
                conn.commit()
        finally:
            engine.dispose()


def _run_with_connection(connection) -> None:
    """Caller owns the connection and its transaction; do not commit here."""
    ensure_rc9_upgrade_allowed(connection)
    _configure(connection)
    with context.begin_transaction():
        context.run_migrations()


def _connection_path(connection) -> Path:
    database = connection.engine.url.database
    if database in {None, ":memory:"}:
        raise RuntimeError("online RC10 migrations require a file-backed SQLite database")
    return Path(database)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
