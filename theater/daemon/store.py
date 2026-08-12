"""SQLite persistence, over SQLAlchemy Core.

Deliberately synchronous. Calls are local, sub-millisecond, and bounded by the
number of participants (tens, not thousands), so running them on the event loop
is cheaper than the complexity of an async driver. Revisit if that stops being
true.

Schema changes go through Alembic (`migrations/versions/`), never through this
file. Up to v1.2 the schema was a `CREATE TABLE IF NOT EXISTS` script replayed
at every start and tracked in `PRAGMA user_version`. That had no ALTER path at
all: adding a column to an existing database was a silent no-op, and the
version guard could not catch it because the version had not changed. It is
why the `jobs` table was created empty two phases before anything wrote to it.
"""

from __future__ import annotations

import json
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, create_engine, event, insert, inspect, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from theater.daemon.schema import bus, jobs, participants
from theater.models import Job, Participant, Status, now

MIGRATIONS = Path(__file__).parent / "migrations"

#: The revision a pre-Alembic database is already at. See `_stamp_legacy`.
BASELINE = "0001"


def _set_pragmas(dbapi_connection, _record) -> None:
    """WAL so a reader never blocks the daemon's writes; foreign keys because
    SQLite disables them per connection, not per database."""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


class Store:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(f"sqlite:///{path}")
        event.listen(self.engine, "connect", _set_pragmas)

        with self.engine.connect() as conn:
            self._stamp_legacy(conn)
            self._upgrade(conn)
            conn.commit()

        # One long-lived autocommit connection, which is what
        # `sqlite3.connect(isolation_level=None)` gave us before: callers never
        # commit, and a write is visible to the next read immediately. The
        # daemon owns this file alone, so there is no second writer to serialise
        # against.
        self.conn = self.engine.connect().execution_options(
            isolation_level="AUTOCOMMIT"
        )

    # ---- migrations ----------------------------------------------------

    def _config(self, conn: Connection) -> Config:
        """An in-memory Alembic config bound to an existing connection.

        Not `alembic.ini`: that file is for the developer CLI and lives at the
        repo root, which is not on disk once Theater is installed as a wheel.
        Both point at the same `script_location`, so they cannot disagree about
        which revisions exist.
        """
        cfg = Config()
        cfg.set_main_option("script_location", str(MIGRATIONS))
        cfg.attributes["connection"] = conn
        return cfg

    def _stamp_legacy(self, conn: Connection) -> None:
        """Adopt a pre-1.3 database instead of rebuilding it.

        Up to v1.2 the schema lived in `PRAGMA user_version` and only ever
        reached version 1, so a legacy file has exactly one possible shape and
        it is the shape of the baseline revision. Stamping is therefore
        truthful, and it preserves the live registry — which pane belongs to
        which participant — across the upgrade. Deleting the file would be two
        lines shorter and would make the daemon forget every running pane.
        """
        tables = set(inspect(conn).get_table_names())
        if "participants" not in tables or "alembic_version" in tables:
            return
        command.stamp(self._config(conn), BASELINE)

    def _upgrade(self, conn: Connection) -> None:
        command.upgrade(self._config(conn), "head")

    def close(self) -> None:
        self.conn.close()
        self.engine.dispose()

    # ---- participants -------------------------------------------------

    def upsert_participant(self, p: Participant) -> None:
        values = {
            "id": p.id,
            "harness": p.harness,
            "tier": str(p.tier),
            "tmux_pane": p.tmux_pane,
            "cwd": p.cwd,
            "branch": p.branch,
            "session_id": p.session_id,
            "parent_id": p.parent_id,
            "pid": p.pid,
            "status": str(p.status),
            "last_activity": p.last_activity,
            "created_at": p.created_at,
        }
        stmt = sqlite_insert(participants).values(**values)
        self.conn.execute(
            stmt.on_conflict_do_update(
                index_elements=[participants.c.id],
                set_={k: v for k, v in values.items() if k != "id"},
            )
        )

    def get_participant(self, pid: str) -> Participant | None:
        row = self.conn.execute(
            select(participants).where(participants.c.id == pid)
        ).first()
        return Participant.from_row(row._mapping) if row else None

    def find_by_pane(self, pane: str) -> Participant | None:
        row = self.conn.execute(
            select(participants)
            .where(participants.c.tmux_pane == pane)
            .where(participants.c.status != str(Status.DEAD))
            .order_by(participants.c.created_at.desc())
            .limit(1)
        ).first()
        return Participant.from_row(row._mapping) if row else None

    def list_participants(self, *, include_dead: bool = False) -> list[Participant]:
        stmt = select(participants)
        if not include_dead:
            stmt = stmt.where(participants.c.status != str(Status.DEAD))
        stmt = stmt.order_by(participants.c.created_at.asc())
        return [Participant.from_row(r._mapping) for r in self.conn.execute(stmt)]

    def children_of(self, pid: str) -> list[Participant]:
        stmt = (
            select(participants)
            .where(participants.c.parent_id == pid)
            .order_by(participants.c.created_at)
        )
        return [Participant.from_row(r._mapping) for r in self.conn.execute(stmt)]

    def set_status(self, pid: str, status: Status) -> None:
        self.conn.execute(
            update(participants)
            .where(participants.c.id == pid)
            .values(status=str(status), last_activity=now())
        )

    def touch(self, pid: str) -> None:
        self.conn.execute(
            update(participants)
            .where(participants.c.id == pid)
            .values(last_activity=now())
        )

    # ---- jobs ----------------------------------------------------------

    def create_job(self, job) -> None:
        self.conn.execute(
            insert(jobs).values(
                handle=job.handle,
                caller_id=job.caller_id,
                target_id=job.target_id,
                kind=job.kind,
                prompt=job.prompt,
                state=job.state,
                result=job.result,
                error_code=job.error_code,
                created_at=job.created_at,
                finished_at=job.finished_at,
            )
        )

    def get_job(self, handle: str) -> Job | None:
        row = self.conn.execute(select(jobs).where(jobs.c.handle == handle)).first()
        return Job.from_row(row._mapping) if row else None

    def finish_job(
        self, handle: str, *, state: str, result: str | None = None,
        error_code: str | None = None, finished_at: float | None = None,
    ) -> None:
        self.conn.execute(
            update(jobs)
            .where(jobs.c.handle == handle)
            .values(
                state=state,
                result=result,
                error_code=error_code,
                finished_at=finished_at,
            )
        )

    def list_jobs_for_caller(self, caller_id: str) -> list[Job]:
        rows = self.conn.execute(
            select(jobs)
            .where(jobs.c.caller_id == caller_id)
            .order_by(jobs.c.created_at.desc())
        ).fetchall()
        return [Job.from_row(r._mapping) for r in rows]

    def running_jobs_for_target(self, target_id: str) -> list[Job]:
        rows = self.conn.execute(
            select(jobs)
            .where(jobs.c.target_id == target_id)
            .where(jobs.c.state == "running")
            .order_by(jobs.c.created_at.desc())
        ).fetchall()
        return [Job.from_row(r._mapping) for r in rows]

    def oldest_running_job_for_target(self, target_id: str) -> Job | None:
        """The longest-running job waiting on this participant, if any.

        Its own query rather than `running_jobs_for_target(...)[-1]`: that one
        orders DESC for display, and a caller in another module relying on the
        sort direction of a query it does not own is a trap. Prompts reach a
        pane in the order they were typed, so the oldest running job is the one
        the next turn answers.

        Jobs created within the same clock tick tie, and the tie breaks
        arbitrarily. That is acceptable — a caller cannot type two prompts into
        one pane at the same instant, so a tie means two different callers
        raced, and neither has a claim on being first.
        """
        row = self.conn.execute(
            select(jobs)
            .where(jobs.c.target_id == target_id)
            .where(jobs.c.state == "running")
            .order_by(jobs.c.created_at.asc())
            .limit(1)
        ).fetchone()
        return Job.from_row(row._mapping) if row else None

    def max_send_seq(self) -> int:
        """Highest numeric suffix across every send handle, 0 if there are none.

        Handles look like `<target_id>#<n>`, so the obvious query — `ORDER BY
        handle DESC LIMIT 1`, which is what the daemon used to run — sorts by
        target id first and lexically within it, where "#9" beats "#10". It
        under-reports the maximum, the restarted daemon then hands out a handle
        that already exists, and the insert dies with `UNIQUE constraint
        failed: jobs.handle`. Read every suffix and take the numeric maximum
        instead; this runs once, at startup, over thousands of rows at most.
        """
        rows = self.conn.execute(
            select(jobs.c.handle).where(jobs.c.handle.like("%#%"))
        ).fetchall()
        best = 0
        for (handle,) in rows:
            _, _, seq = handle.rpartition("#")
            if seq.isdigit():
                best = max(best, int(seq))
        return best

    # ---- bus ----------------------------------------------------------

    def bus_append(
        self, kind: str, *, from_id: str | None = None,
        to_id: str | None = None, payload: dict | None = None,
    ) -> int:
        result = self.conn.execute(
            insert(bus).values(
                ts=now(),
                from_id=from_id,
                to_id=to_id,
                kind=kind,
                payload=json.dumps(payload) if payload else None,
            )
        )
        return result.inserted_primary_key[0]

    def bus_tail(self, limit: int = 100, *, after_id: int = 0) -> list[dict]:
        rows = self.conn.execute(
            select(bus)
            .where(bus.c.id > after_id)
            .order_by(bus.c.id.desc())
            .limit(limit)
        ).fetchall()
        out = []
        for r in reversed(rows):
            d = dict(r._mapping)
            d["payload"] = json.loads(d["payload"]) if d["payload"] else None
            out.append(d)
        return out
