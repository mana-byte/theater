"""Job CRUD, running-job queries, and max-send-seq."""

from __future__ import annotations

from sqlalchemy import func, insert, select, update

from theater.daemon.persistence.database import Database
from theater.daemon.schema import jobs
from theater.models import Job, JobState


class JobRepository:
    """Reads and writes the ``jobs`` table via ``db.conn``."""

    def __init__(self, db: Database):
        self._db = db

    def create(self, job) -> None:
        self._db.conn.execute(
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

    def get(self, handle: str) -> Job | None:
        row = self._db.conn.execute(select(jobs).where(jobs.c.handle == handle)).first()
        return Job.from_row(row._mapping) if row else None

    def finish(
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
        self._db.conn.execute(
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

    def running_for_target(self, target_id: str) -> list[Job]:
        rows = self._db.conn.execute(
            select(jobs)
            .where(jobs.c.target_id == target_id)
            .where(jobs.c.state == "running")
            .order_by(jobs.c.created_at.desc())
        ).fetchall()
        return [Job.from_row(r._mapping) for r in rows]

    def oldest_running_for_target(self, target_id: str) -> Job | None:
        """The longest-running job waiting on this participant, if any."""
        row = self._db.conn.execute(
            select(jobs)
            .where(jobs.c.target_id == target_id)
            .where(jobs.c.state == "running")
            .order_by(jobs.c.created_at.asc())
            .limit(1)
        ).fetchone()
        return Job.from_row(row._mapping) if row else None

    def max_send_seq(self) -> int:
        """Highest numeric suffix across every send handle, 0 if none."""
        rows = self._db.conn.execute(
            select(jobs.c.handle).where(jobs.c.handle.like("%#%"))
        ).fetchall()
        best = 0
        for (handle,) in rows:
            _, _, seq = handle.rpartition("#")
            if seq.isdigit():
                best = max(best, int(seq))
        return best

    def spawn_prompts_for_targets(self, ids: list[str]) -> dict[str, str | None]:
        if not ids:
            return {}
        rows = self._db.conn.execute(
            select(jobs.c.target_id, jobs.c.prompt)
            .where(jobs.c.target_id.in_(ids))
            .where(jobs.c.kind == "spawn")
            .order_by(jobs.c.created_at.asc())
        )
        prompts: dict[str, str | None] = {}
        for target_id, prompt in rows:
            prompts.setdefault(target_id, prompt)
        return prompts

    def active_count(self) -> int:
        """Count of jobs whose persisted state is ``running``."""
        return int(
            self._db.conn.execute(
                select(func.count()).select_from(jobs).where(jobs.c.state == str(JobState.RUNNING))
            ).scalar_one()
        )
