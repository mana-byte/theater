"""Job rows plus the persisted meta key/value and send-sequence counter."""

from __future__ import annotations

from collections.abc import Sequence

from theater.daemon.events.publication import job_event, next_revision
from theater.daemon.persistence.store_parts._host import StoreHost
from theater.models import Job, now


class JobStore(StoreHost):
    """Store-facing jobs methods; state lives on ``Store``."""

    def create_job(self, job, *, connection=None) -> None:
        if connection is not None:
            self._jobs.create(job, connection=connection)
            return
        with self.write_unit() as unit:
            self._jobs.create(job, connection=unit.connection)
            persisted = self._jobs.get(job.handle, connection=unit.connection)
            assert persisted is not None
            self.journal.append_group(
                unit,
                [
                    job_event(
                        persisted,
                        revision=next_revision(self, unit.connection),
                        recorded_at=persisted.created_at,
                    )
                ],
            )

    def get_job(self, handle: str, *, connection=None) -> Job | None:
        return self._jobs.get(handle, connection=connection)

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
        connection=None,
    ) -> None:
        if connection is not None:
            self._jobs.finish(
                handle,
                state=state,
                result=result,
                error_code=error_code,
                finished_at=finished_at,
                response_format=response_format,
                structured_result=structured_result,
                structured_status=structured_status,
                connection=connection,
            )
            return
        with self.write_unit() as unit:
            before = self._jobs.get(handle, connection=unit.connection)
            self._jobs.finish(
                handle,
                state=state,
                result=result,
                error_code=error_code,
                finished_at=finished_at,
                response_format=response_format,
                structured_result=structured_result,
                structured_status=structured_status,
                connection=unit.connection,
            )
            current = self._jobs.get(handle, connection=unit.connection)
            if current is not None and current != before:
                self.journal.append_group(
                    unit,
                    [
                        job_event(
                            current,
                            revision=next_revision(self, unit.connection),
                            recorded_at=current.finished_at or now(),
                        )
                    ],
                )

    def running_jobs_for_target(self, target_id: str) -> list[Job]:
        return self._jobs.running_for_target(target_id)

    def oldest_running_job_for_target(self, target_id: str) -> Job | None:
        """The longest-running job waiting on this participant, if any."""
        return self._jobs.oldest_running_for_target(target_id)

    def max_send_seq(self) -> int:
        """Highest numeric suffix across every send handle, 0 if none."""
        return self._jobs.max_send_seq()

    def spawn_prompts_for_targets(self, ids: Sequence[str]) -> dict[str, str | None]:
        return self._jobs.spawn_prompts_for_targets(list(ids))

    def active_job_count(self) -> int:
        """Count of jobs whose persisted state is ``running``."""
        return self._jobs.active_count()

    # ---- meta -----------------------------------------------------------

    def get_meta(self, key: str, *, connection=None) -> str | None:
        return self._meta.get(key, connection=connection)

    def set_meta(self, key: str, value: str, *, connection=None) -> None:
        self._meta.set(key, value, connection=connection)

    def get_send_seq(self) -> int:
        return self._meta.get_send_seq()

    def set_send_seq(self, value: int) -> None:
        self._meta.set_send_seq(value)
