"""Token-usage rows and turn/refusal statistics."""

from __future__ import annotations

from theater.daemon.persistence.store_parts._host import StoreHost


class UsageStore(StoreHost):
    """Store-facing usage methods; state lives on ``Store``."""

    def turn_outcomes(self, *, since: float | None = None) -> list[dict]:
        """How each harness's turns ended, counted per harness."""
        return self._statistics.turn_outcomes(since=since)

    def refusal_counts(self, *, since: float | None = None) -> dict[str, int]:
        """Sends refused before a job existed, counted by reason."""
        return self._bus.refusal_counts(since=since)

    # ---- usage ----------------------------------------------------------

    def record_usage(
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
        return self._usage.record(
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

    def usage_totals(self, *, since: float | None = None) -> dict:
        """Sum of all token and cost columns across the usage table."""
        return self._usage.totals(since=since)

    def usage_summary(self, *, since: float, average_since: float) -> dict[str, dict]:
        """All-time and two windowed usage totals in one table scan."""
        return self._usage.summary(since=since, average_since=average_since)

    def usage_by_participant(
        self,
        *,
        since: float | None = None,
        participant_ids: list[str] | None = None,
        limit: int = 500,
    ) -> dict[str, object]:
        """Aggregate usage by participant, optionally filtered by time and ID."""
        return self._usage.by_participant(
            since=since,
            participant_ids=participant_ids,
            limit=limit,
        )

    def usage_by_harness(
        self, *, day_since: float, week_since: float, month_since: float
    ) -> list[dict]:
        """Aggregate the three local-calendar usage periods by durable harness."""
        return self._usage.by_harness(
            day_since=day_since, week_since=week_since, month_since=month_since
        )

    def usage_by_harness_detailed(
        self, *, day_since: float, week_since: float, month_since: float
    ) -> dict:
        """Aggregate the displayed periods by harness, model, and global total."""
        return self._usage.by_harness_detailed(
            day_since=day_since, week_since=week_since, month_since=month_since
        )
