"""Turn-outcome aggregation (metrics)."""

from __future__ import annotations

from sqlalchemy import ColumnElement, case, func, select

from theater.daemon.persistence.database import Database
from theater.daemon.schema import jobs, participants


class StatisticsRepository:
    """Aggregation queries over ``jobs`` and ``participants`` via ``db.conn``."""

    def __init__(self, db: Database):
        self._db = db

    def turn_outcomes(self, *, since: float | None = None) -> list[dict]:
        """How each harness's turns ended, counted per harness.

        A "turn" is a job that carried a prompt. Left join: a job whose
        target has been forgotten still counts under "unknown".
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
        return [dict(r._mapping) for r in self._db.conn.execute(query).fetchall()]
