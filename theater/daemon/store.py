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

import contextlib
import json
from collections.abc import Sequence
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import (
    ColumnElement,
    Connection,
    case,
    create_engine,
    delete,
    event,
    func,
    insert,
    inspect,
    select,
    update,
)
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from theater.daemon.schema import (
    bus,
    jobs,
    meta,
    named_worktrees,
    participants,
    tree_kv,
)
from theater.models import Job, Participant, Status, new_id, now
from theater.provenance import TranscriptProvenance

MIGRATIONS = Path(__file__).parent / "migrations"

#: The revision a pre-Alembic database is already at. See `_stamp_legacy`.
BASELINE = "0001"

#: The latest revision. A legacy database is stamped at BASELINE and then
#: upgraded to this; a fresh database lands here directly. Tests assert
#: against this rather than hardcoding a revision string.
HEAD = "0015"
RECEIPT_TOKEN_PREFIX = "receipt_token:"


def _set_pragmas(dbapi_connection, _record) -> None:
    """WAL so a reader never blocks the daemon's writes; foreign keys because
    SQLite disables them per connection, not per database; busy_timeout so a
    writer that cannot acquire the lock immediately waits up to 5s rather than
    failing instantly.

    The daemon owns this file alone, so contention is not between two
    daemons — it is between the long-lived autocommit connection and the
    fresh transactional connections that `_finish_with_touches` opens. Both
    write, and without a busy_timeout the second writer fails with
    SQLITE_BUSY if it happens to land while the first is mid-commit. Five
    seconds is long enough to absorb any realistic hold (a single SQLite
    write is sub-millisecond) and short enough that a genuinely stuck lock —
    which would mean something is wrong with the database file or the
    filesystem — surfaces as an error rather than hanging the daemon's event
    loop indefinitely."""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA busy_timeout=5000")
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

        # One long-lived autocommit connection: callers never commit,
        # and a write is visible to the next read immediately. The
        # daemon owns this file alone, so there is no second writer.
        self.conn = self.engine.connect().execution_options(isolation_level="AUTOCOMMIT")

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
        values = self._participant_values(p)
        stmt = sqlite_insert(participants).values(**values)
        self.conn.execute(
            stmt.on_conflict_do_update(
                index_elements=[participants.c.id],
                set_={k: v for k, v in values.items() if k != "id"},
            )
        )

    @staticmethod
    def _participant_values(p: Participant) -> dict:
        return {
            "id": p.id,
            "harness": p.harness,
            "tier": str(p.tier),
            "tmux_pane": p.tmux_pane,
            "cwd": p.cwd,
            "branch": p.branch,
            "session_id": p.session_id,
            "session_correlation": p.session_correlation,
            "transcript_domain": p.transcript_domain,
            "transcript_location": p.transcript_location,
            "resume_floor": p.resume_floor,
            "parent_id": p.parent_id,
            "pid": p.pid,
            "status": str(p.status),
            "last_activity": p.last_activity,
            "created_at": p.created_at,
        }

    def bind_operator_transcript(
        self,
        *,
        target: Participant,
        prior_owner: Participant | None,
        audit_payload: dict,
    ) -> int:
        """Move transcript ownership and append the audit row atomically."""
        target_values = self._participant_values(target)
        with self.engine.begin() as conn:
            if prior_owner is not None:
                conn.execute(
                    update(participants)
                    .where(participants.c.id == prior_owner.id)
                    .values(
                        session_id=None,
                        session_correlation=None,
                        transcript_location=None,
                    )
                )
                conn.execute(
                    insert(bus).values(
                        ts=now(),
                        from_id="cli",
                        to_id=prior_owner.id,
                        kind="operator.transcript_unbind",
                        payload=json.dumps(
                            {
                                "actor_surface": "cli",
                                "target": prior_owner.id,
                                "transferred_to": target.id,
                                "path": audit_payload.get("path"),
                            }
                        ),
                    )
                )
            conn.execute(
                sqlite_insert(participants)
                .values(**target_values)
                .on_conflict_do_update(
                    index_elements=[participants.c.id],
                    set_={k: v for k, v in target_values.items() if k != "id"},
                )
            )
            result = conn.execute(
                insert(bus).values(
                    ts=now(),
                    from_id="cli",
                    to_id=target.id,
                    kind="operator.transcript_bind",
                    payload=json.dumps(audit_payload),
                )
            )
            pk = result.inserted_primary_key
            assert pk is not None
            return pk[0]

    def get_participant(self, pid: str) -> Participant | None:
        row = self.conn.execute(select(participants).where(participants.c.id == pid)).first()
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

    def list_participants(
        self,
        *,
        include_dead: bool = False,
        ids: Sequence[str] | None = None,
    ) -> list[Participant]:
        stmt = select(participants)
        if not include_dead:
            stmt = stmt.where(participants.c.status != str(Status.DEAD))
        if ids is not None:
            if not ids:
                return []
            stmt = stmt.where(participants.c.id.in_(ids))
        stmt = stmt.order_by(participants.c.created_at.asc())
        return [Participant.from_row(r._mapping) for r in self.conn.execute(stmt)]

    def list_recent_dead(
        self, *, limit: int = 20, exclude_session_ids: set[str] | None = None
    ) -> list[Participant]:
        stmt = (
            select(participants)
            .where(participants.c.status == str(Status.DEAD))
            .where(participants.c.session_id.is_not(None))
            .where(participants.c.session_id != "")
        )
        if exclude_session_ids:
            stmt = stmt.where(participants.c.session_id.not_in(exclude_session_ids))
        stmt = stmt.order_by(
            participants.c.last_activity.desc(),
            participants.c.created_at.desc(),
            participants.c.id.desc(),
        ).limit(limit)
        return [Participant.from_row(r._mapping) for r in self.conn.execute(stmt)]

    def spawn_prompts_for_targets(self, ids: Sequence[str]) -> dict[str, str | None]:
        if not ids:
            return {}
        rows = self.conn.execute(
            select(jobs.c.target_id, jobs.c.prompt)
            .where(jobs.c.target_id.in_(ids))
            .where(jobs.c.kind == "spawn")
            .order_by(jobs.c.created_at.asc())
        )
        prompts: dict[str, str | None] = {}
        for target_id, prompt in rows:
            prompts.setdefault(target_id, prompt)
        return prompts

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
            update(participants).where(participants.c.id == pid).values(last_activity=now())
        )

    def clear_resume_floor(self, pid: str) -> None:
        """Clear the resume floor column without touching any other field.

        A targeted single-column update, not a whole-row upsert: the caller
        may hold a stale ``Participant`` snapshot taken before ``_settle``
        moved status/last_activity, and replaying that snapshot would revert
        those changes. This method writes only ``resume_floor = NULL``.
        """
        self.conn.execute(
            update(participants).where(participants.c.id == pid).values(resume_floor=None)
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
                response_format=getattr(job, "response_format", None),
                structured_result=getattr(job, "structured_result", None),
                structured_status=getattr(job, "structured_status", None),
            )
        )

    def get_job(self, handle: str) -> Job | None:
        row = self.conn.execute(select(jobs).where(jobs.c.handle == handle)).first()
        return Job.from_row(row._mapping) if row else None

    def finish_job(
        self,
        handle: str,
        *,
        state: str,
        result: str | None = None,
        error_code: str | None = None,
        finished_at: float | None = None,
        response_format: str | None = None,
        structured_result: str | None = None,
        structured_status: str | None = None,
    ) -> None:
        self.conn.execute(
            update(jobs)
            .where(jobs.c.handle == handle)
            .values(
                state=state,
                result=result,
                error_code=error_code,
                finished_at=finished_at,
                response_format=response_format,
                structured_result=structured_result,
                structured_status=structured_status,
            )
        )

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
        rows = self.conn.execute(select(jobs.c.handle).where(jobs.c.handle.like("%#%"))).fetchall()
        best = 0
        for (handle,) in rows:
            _, _, seq = handle.rpartition("#")
            if seq.isdigit():
                best = max(best, int(seq))
        return best

    # ---- meta -----------------------------------------------------------

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute(select(meta.c.value).where(meta.c.key == key)).first()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        stmt = sqlite_insert(meta).values(key=key, value=value)
        self.conn.execute(
            stmt.on_conflict_do_update(
                index_elements=[meta.c.key],
                set_={"value": value},
            )
        )

    def get_send_seq(self) -> int:
        raw = self.get_meta("send_seq")
        if raw is None:
            return 0
        try:
            return int(raw)
        except ValueError:
            return 0

    def set_send_seq(self, value: int) -> None:
        self.set_meta("send_seq", str(value))

    def set_receipt_token(
        self,
        participant_id: str,
        token: str,
        *,
        token_path: str | None = None,
    ) -> None:
        payload: dict[str, object] = {
            "token": token,
            "token_path": token_path,
        }
        self.set_meta(f"{RECEIPT_TOKEN_PREFIX}{participant_id}", json.dumps(payload))

    def get_receipt_token(self, participant_id: str) -> str | None:
        participant = self.get_participant(participant_id)
        if participant is None or participant.status is Status.DEAD:
            self.delete_receipt_token(participant_id)
            return None
        payload = self._receipt_token_payload(participant_id)
        if payload is None:
            return None
        token = payload.get("token")
        return token if isinstance(token, str) else None

    def renew_receipt_token(self, participant_id: str) -> None:
        payload = self._receipt_token_payload(participant_id)
        if payload is None:
            return
        token = payload.get("token")
        if not isinstance(token, str):
            return
        token_path = payload.get("token_path")
        self.set_receipt_token(
            participant_id,
            token,
            token_path=token_path if isinstance(token_path, str) else None,
        )

    def delete_receipt_token(self, participant_id: str) -> None:
        payload = self._receipt_token_payload(participant_id)
        token_path = payload.get("token_path") if payload is not None else None
        if isinstance(token_path, str) and token_path:
            with contextlib.suppress(OSError):
                Path(token_path).unlink(missing_ok=True)
        self.conn.execute(
            delete(meta).where(meta.c.key == f"{RECEIPT_TOKEN_PREFIX}{participant_id}")
        )

    def cleanup_receipt_tokens(self) -> int:
        rows = self.conn.execute(
            select(meta.c.key, meta.c.value).where(meta.c.key.like(f"{RECEIPT_TOKEN_PREFIX}%"))
        ).fetchall()
        deleted = 0
        for key, _raw in rows:
            participant_id = key.removeprefix(RECEIPT_TOKEN_PREFIX)
            participant = self.get_participant(participant_id)
            if participant is not None and participant.status is not Status.DEAD:
                continue
            self.delete_receipt_token(participant_id)
            deleted += 1
        return deleted

    def _receipt_token_payload(self, participant_id: str) -> dict | None:
        return self._decode_receipt_token(self.get_meta(f"{RECEIPT_TOKEN_PREFIX}{participant_id}"))

    @staticmethod
    def _decode_receipt_token(raw: str | None) -> dict | None:
        if raw is None:
            return None
        try:
            payload = json.loads(raw)
        except ValueError:
            return {"token": raw, "expires_at": 0}
        return payload if isinstance(payload, dict) else None

    def record_transcript_receipt(
        self,
        participant_id: str,
        *,
        session_id: str,
        transcript_location: str,
    ) -> Participant | None:
        """Atomically persist exact receipt provenance for a participant."""
        with self.engine.begin() as conn:
            conn.execute(
                update(participants)
                .where(participants.c.id == participant_id)
                .values(
                    session_id=session_id,
                    session_correlation=str(TranscriptProvenance.EXACT),
                    transcript_location=transcript_location,
                )
            )
            row = conn.execute(
                select(participants).where(participants.c.id == participant_id)
            ).first()
        return Participant.from_row(row._mapping) if row else None

    # ---- scratchpad -----------------------------------------------------

    def scratchpad_write(
        self,
        *,
        tree_root_id: str,
        repo_root: str,
        namespace: str,
        value: str,
        updated_by: str,
        key: str | None = None,
    ) -> str:
        if key is None:
            key = new_id()
            stmt = sqlite_insert(tree_kv).values(
                tree_root_id=tree_root_id,
                repo_root=repo_root,
                namespace=namespace,
                key=key,
                value=value,
                updated_at=now(),
                updated_by=updated_by,
            )
            self.conn.execute(stmt)
        else:
            existing = self.conn.execute(
                select(tree_kv.c.key)
                .where(tree_kv.c.tree_root_id == tree_root_id)
                .where(tree_kv.c.repo_root == repo_root)
                .where(tree_kv.c.namespace == namespace)
                .where(tree_kv.c.key == key)
            ).first()
            if existing:
                self.conn.execute(
                    tree_kv.update()
                    .where(tree_kv.c.tree_root_id == tree_root_id)
                    .where(tree_kv.c.repo_root == repo_root)
                    .where(tree_kv.c.namespace == namespace)
                    .where(tree_kv.c.key == key)
                    .values(value=value, updated_at=now(), updated_by=updated_by)
                )
            else:
                self.conn.execute(
                    sqlite_insert(tree_kv).values(
                        tree_root_id=tree_root_id,
                        repo_root=repo_root,
                        namespace=namespace,
                        key=key,
                        value=value,
                        updated_at=now(),
                        updated_by=updated_by,
                    )
                )
        return key

    def scratchpad_get(
        self,
        *,
        tree_root_id: str,
        repo_root: str,
        namespace: str,
        keys: list[str] | None = None,
    ) -> dict[str, str]:
        stmt = (
            select(tree_kv.c.key, tree_kv.c.value)
            .where(tree_kv.c.tree_root_id == tree_root_id)
            .where(tree_kv.c.repo_root == repo_root)
            .where(tree_kv.c.namespace == namespace)
        )
        if keys is not None:
            stmt = stmt.where(tree_kv.c.key.in_(keys))
        rows = self.conn.execute(stmt).fetchall()
        return {row[0]: row[1] for row in rows}

    def reparent_participant(self, pid: str, *, new_parent_id: str) -> None:
        """Set the parent_id of a participant.

        Daemon-only write; the registry layer checks lineage before calling
        this to avoid cycles. No bus event is emitted here — reparenting is
        internal orchestration bookkeeping, not a user-visible state change.
        """
        self.conn.execute(
            update(participants).where(participants.c.id == pid).values(parent_id=new_parent_id)
        )

    # ---- named worktrees ------------------------------------------------

    def get_named_worktree(self, *, repo_root: str, name: str) -> dict | None:
        row = self.conn.execute(
            select(named_worktrees)
            .where(named_worktrees.c.repo_root == repo_root)
            .where(named_worktrees.c.name == name)
        ).first()
        return dict(row._mapping) if row else None

    def upsert_named_worktree(
        self,
        *,
        repo_root: str,
        name: str,
        branch: str,
        path: str,
        base_branch: str | None,
    ) -> None:
        stmt = sqlite_insert(named_worktrees).values(
            repo_root=repo_root,
            name=name,
            branch=branch,
            path=path,
            base_branch=base_branch,
            created_at=now(),
        )
        self.conn.execute(
            stmt.on_conflict_do_update(
                index_elements=[named_worktrees.c.repo_root, named_worktrees.c.name],
                set_={
                    "branch": branch,
                    "path": path,
                    "base_branch": base_branch,
                },
            )
        )

    def delete_named_worktree(self, *, repo_root: str, name: str) -> None:
        self.conn.execute(
            named_worktrees.delete()
            .where(named_worktrees.c.repo_root == repo_root)
            .where(named_worktrees.c.name == name)
        )

    def named_worktree_by_path(self, path: str) -> dict | None:
        row = self.conn.execute(
            select(named_worktrees).where(named_worktrees.c.path == path)
        ).first()
        return dict(row._mapping) if row else None

    def live_participants_in_cwd(self, cwd: str) -> list[Participant]:
        rows = self.conn.execute(
            select(participants)
            .where(participants.c.cwd == cwd)
            .where(participants.c.status != str(Status.DEAD))
        ).fetchall()
        return [Participant.from_row(r._mapping) for r in rows]

    # ---- metrics --------------------------------------------------------

    def turn_outcomes(self, *, since: float | None = None) -> list[dict]:
        """How each harness's turns ended, counted per harness.

        A "turn" here is a job that carried a prompt — a spawn prompt occupies
        a pane exactly as much as a send does, and both are answered by the
        same observer path, so both are evidence about whether that path works.

        The counts come out of the jobs table rather than from counters kept in
        the observer, because the table already records the distinction and
        survives a restart. `error_code` is the whole signal: the observer sets
        it to `turn_end_unseen` when it gives up waiting for a turn boundary
        and returns the last thing the agent was heard to say. That is a
        *silent* degradation — the caller gets a plausible answer and nothing
        anywhere says it was salvaged — which is exactly why it needs counting.

        Left join: a job whose target has since been forgotten still counts,
        under "unknown", rather than vanishing and flattering the numbers.
        """
        src = jobs.join(participants, jobs.c.target_id == participants.c.id, isouter=True)

        def total(condition) -> ColumnElement[int]:
            return func.sum(case((condition, 1), else_=0))

        query = (
            select(
                func.coalesce(participants.c.harness, "unknown").label("harness"),
                func.count().label("turns"),
                total((jobs.c.state == "done") & (jobs.c.error_code.is_(None))).label("clean"),
                total(jobs.c.error_code == "turn_end_unseen").label("rescued"),
                total(jobs.c.state == "crashed").label("failed"),
                total(jobs.c.state == "running").label("running"),
            )
            .select_from(src)
            .where(jobs.c.prompt.is_not(None))
            .group_by("harness")
            .order_by("harness")
        )
        if since is not None:
            query = query.where(jobs.c.created_at >= since)
        return [dict(r._mapping) for r in self.conn.execute(query).fetchall()]

    def refusal_counts(self, *, since: float | None = None) -> dict[str, int]:
        """Sends refused before a job existed, counted by reason.

        These leave no job row on purpose — nothing was reserved, so there is
        nothing to close — which is why they are counted off the bus instead.
        Aggregating in Python rather than in SQL because the reason lives
        inside a JSON payload, and a query that reaches into it would bind this
        to SQLite's JSON support for no gain: refusals are rare.
        """
        query = select(bus.c.payload).where(bus.c.kind == "send.refused")
        if since is not None:
            query = query.where(bus.c.ts >= since)
        counts: dict[str, int] = {}
        for (payload,) in self.conn.execute(query).fetchall():
            try:
                reason = json.loads(payload or "{}").get("reason") or "unknown"
            except ValueError:
                reason = "unknown"
            counts[reason] = counts.get(reason, 0) + 1
        return counts

    # ---- bus ----------------------------------------------------------

    def bus_append(
        self,
        kind: str,
        *,
        from_id: str | None = None,
        to_id: str | None = None,
        payload: dict | None = None,
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
        pk = result.inserted_primary_key
        assert pk is not None
        return pk[0]

    def bus_tail(self, limit: int = 100, *, after_id: int = 0) -> list[dict]:
        rows = self.conn.execute(
            select(bus).where(bus.c.id > after_id).order_by(bus.c.id.desc()).limit(limit)
        ).fetchall()
        out = []
        for r in reversed(rows):
            d = dict(r._mapping)
            d["payload"] = json.loads(d["payload"]) if d["payload"] else None
            out.append(d)
        return out

    def observation_error_active(self, participant_id: str, code: str) -> bool:
        """Whether an observation error remains uncleared in the audit stream.

        This is not a state column. A watcher calls this once at lifecycle start
        to rebuild its cache; predicates never call it. The bus records the
        transition so a daemon restart does not forget a quarantine before the
        operator rebinds.
        """
        return self._observation_error_row(participant_id, code) is not None

    def observation_error_timestamp(self, participant_id: str, code: str) -> float | None:
        """The wall-clock ``ts`` of the most recent uncleared observation error.

        Returns ``None`` when the error is not active. Used by the observer on
        restart replay to avoid resetting ``failed_at`` to ``now()`` — which
        would grant endless fresh grace to a quarantine that persisted across
        a daemon restart.
        """
        row = self._observation_error_row(participant_id, code)
        return row["ts"] if row is not None else None

    def _observation_error_row(self, participant_id: str, code: str) -> dict | None:
        rows = self.conn.execute(
            select(bus.c.kind, bus.c.payload, bus.c.ts)
            .where(bus.c.to_id == participant_id)
            .where(
                bus.c.kind.in_(
                    [
                        "agent.observation_error",
                        "agent.transcript",
                        "agent.transcript_receipt",
                        "operator.transcript_bind",
                        "operator.transcript_unbind",
                    ]
                )
            )
            .order_by(bus.c.id.desc())
        ).fetchall()
        for row in rows:
            kind = row.kind
            payload = row.payload
            if kind in {
                "agent.transcript",
                "operator.transcript_bind",
                "operator.transcript_unbind",
            }:
                return None
            try:
                decoded = json.loads(payload or "{}")
            except ValueError:
                continue
            if kind == "agent.transcript_receipt" and decoded.get("admission") == "accepted":
                return None
            found = decoded.get("code")
            if found == code:
                return {"ts": row.ts, "kind": kind, "payload": decoded}
        return None
