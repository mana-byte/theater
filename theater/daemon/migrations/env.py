"""Alembic environment.

Two callers share this file:

  * the daemon, which runs `command.upgrade(cfg, "head")` while constructing a
    `Store` and hands its live `Connection` over in `config.attributes`;
  * a developer running `uv run alembic revision --autogenerate -m "..."` from
    the repo root, where there is no connection and the URL is resolved from
    `$THEATER_HOME` so the CLI and the daemon always migrate the same file.

`render_as_batch` is the setting that matters here. SQLite has no real ALTER
TABLE: dropping a column, changing a type, or renaming under a constraint is
simply not expressible. Batch mode makes Alembic create a new table, copy the
rows, and swap the names. That capability is the entire reason this directory
exists — the pre-1.3 `executescript(SCHEMA)` could only ever add whole tables.
"""

from __future__ import annotations

from alembic import context
from sqlalchemy import create_engine

from theater import paths
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
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        render_as_batch=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connection = context.config.attributes.get("connection")
    if connection is not None:
        # Caller owns the connection and its transaction; do not commit here.
        _configure(connection)
        with context.begin_transaction():
            context.run_migrations()
        return

    paths.ensure_home()
    paths.ensure_private_file(paths.db_path())
    engine = create_engine(_url())
    try:
        with engine.connect() as conn:
            _configure(conn)
            with context.begin_transaction():
                context.run_migrations()
            conn.commit()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
