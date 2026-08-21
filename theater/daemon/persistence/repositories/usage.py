"""Usage recording and aggregation queries."""

from __future__ import annotations

from sqlalchemy import ColumnElement, case, distinct, func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from theater.daemon.persistence.database import Database
from theater.daemon.schema import usage


class UsageRepository:
    """Reads and writes the ``usage`` table via ``db.conn``."""

    def __init__(self, db: Database):
        self._db = db

    def record(
        self,
        *,
        participant_id: str,
        tree_root_id: str | None,
        usage_key: str | None,
        ts: float,
        model: str | None,
        harness: str,
        input_tokens: int,
        output_tokens: int,
        cache_creation_input_tokens: int,
        cache_read_input_tokens: int,
        reasoning_output_tokens: int,
        cost_microcents: int,
    ) -> bool:
        """Insert one usage row, returning whether its native key was new."""
        statement = sqlite_insert(usage).values(
            participant_id=participant_id,
            tree_root_id=tree_root_id,
            usage_key=usage_key,
            ts=ts,
            model=model,
            harness=harness,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_creation_input_tokens=cache_creation_input_tokens,
            cache_read_input_tokens=cache_read_input_tokens,
            reasoning_output_tokens=reasoning_output_tokens,
            cost_microcents=cost_microcents,
        )
        if usage_key is not None:
            statement = statement.on_conflict_do_nothing(
                index_elements=[usage.c.participant_id, usage.c.usage_key]
            )
        result = self._db.conn.execute(statement)
        return result.rowcount > 0

    def totals(self, *, since: float | None = None) -> dict:
        """Sum of all token and cost columns across the usage table."""
        query = select(
            func.coalesce(func.sum(usage.c.input_tokens), 0).label("input_tokens"),
            func.coalesce(func.sum(usage.c.output_tokens), 0).label("output_tokens"),
            func.coalesce(func.sum(usage.c.cache_creation_input_tokens), 0).label(
                "cache_creation_input_tokens"
            ),
            func.coalesce(func.sum(usage.c.cache_read_input_tokens), 0).label(
                "cache_read_input_tokens"
            ),
            func.coalesce(func.sum(usage.c.reasoning_output_tokens), 0).label(
                "reasoning_output_tokens"
            ),
            func.coalesce(func.sum(usage.c.cost_microcents), 0).label("cost_microcents"),
        )
        if since is not None:
            query = query.where(usage.c.ts >= since)
        row = self._db.conn.execute(query).fetchone()
        assert row is not None
        return dict(row._mapping)

    def summary(self, *, since: float, average_since: float) -> dict[str, dict]:
        """All-time and two windowed usage totals in one table scan."""
        columns = {
            "input_tokens": usage.c.input_tokens,
            "output_tokens": usage.c.output_tokens,
            "cache_creation_input_tokens": usage.c.cache_creation_input_tokens,
            "cache_read_input_tokens": usage.c.cache_read_input_tokens,
            "reasoning_output_tokens": usage.c.reasoning_output_tokens,
            "cost_microcents": usage.c.cost_microcents,
        }
        selected: list[ColumnElement] = []
        for name, column in columns.items():
            selected.extend(
                (
                    func.coalesce(func.sum(column), 0).label(f"all_time_{name}"),
                    func.coalesce(func.sum(case((usage.c.ts >= since, column), else_=0)), 0).label(
                        f"windowed_{name}"
                    ),
                    func.coalesce(
                        func.sum(case((usage.c.ts >= average_since, column), else_=0)), 0
                    ).label(f"average_{name}"),
                )
            )
        local_date = func.date(usage.c.ts, "unixepoch", "localtime")
        selected.append(
            func.count(distinct(case((usage.c.ts >= average_since, local_date)))).label(
                "average_active_days"
            )
        )
        row = self._db.conn.execute(select(*selected)).fetchone()
        assert row is not None
        values = row._mapping
        result = {
            group: {name: values[f"{group}_{name}"] for name in columns}
            for group in ("all_time", "windowed", "average")
        }
        result["average"]["active_days"] = values["average_active_days"]
        return result

    def by_harness(self, *, day_since: float, week_since: float, month_since: float) -> list[dict]:
        """Aggregate the three local-calendar usage periods by durable harness."""
        periods = {
            "today": day_since,
            "week": week_since,
            "month": month_since,
        }
        columns = {
            "input_tokens": usage.c.input_tokens,
            "output_tokens": usage.c.output_tokens,
            "cache_creation_input_tokens": usage.c.cache_creation_input_tokens,
            "cache_read_input_tokens": usage.c.cache_read_input_tokens,
            "reasoning_output_tokens": usage.c.reasoning_output_tokens,
            "cost_microcents": usage.c.cost_microcents,
        }
        local_date = func.date(usage.c.ts, "unixepoch", "localtime")
        selected: list[ColumnElement] = [usage.c.harness]
        for period, since in periods.items():
            for name, column in columns.items():
                selected.append(
                    func.coalesce(func.sum(case((usage.c.ts >= since, column), else_=0)), 0).label(
                        f"{period}_{name}"
                    )
                )
            selected.append(
                func.count(distinct(case((usage.c.ts >= since, local_date)))).label(
                    f"{period}_active_days"
                )
            )

        lower_bound = min(week_since, month_since)
        rows = self._db.conn.execute(
            select(*selected).where(usage.c.ts >= lower_bound).group_by(usage.c.harness)
        ).fetchall()
        result: list[dict] = []
        for row in rows:
            values = row._mapping
            result.append(
                {
                    "harness": values["harness"],
                    **{
                        period: {
                            **{name: values[f"{period}_{name}"] for name in columns},
                            "active_days": values[f"{period}_active_days"],
                        }
                        for period in periods
                    },
                }
            )
        return result
