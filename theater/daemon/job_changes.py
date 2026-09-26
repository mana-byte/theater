"""What a finished job changed, from the touch rows committed with its result.

Only paths the harness reported are visible; shell edits are not, so an empty
record reads as "no evidence", never as "no changes".
"""

from __future__ import annotations

from sqlalchemy import select

from theater.constants.daemon import JOB_CHANGES_MAX_PATHS
from theater.daemon.schema import touch
from theater.daemon.store import Store
from theater.models import Job, JobState

EVIDENCE_TOOL_PATHS = "tool_paths"
EVIDENCE_NONE = "none"


def job_changes(store: Store, job: Job) -> dict[str, object] | None:
    """A bounded summary of a terminal job's observed file changes; None while it runs."""
    if job.state == JobState.RUNNING:
        return None
    rows = store.conn.execute(
        select(touch.c.path, touch.c.sha_before, touch.c.sha_after, touch.c.sha_after_error).where(
            touch.c.job_handle == job.handle
        )
    ).all()
    if not rows:
        return {"evidence": EVIDENCE_NONE}
    modified = [
        row.path for row in rows if row.sha_after_error is None and row.sha_before != row.sha_after
    ]
    return {
        "evidence": EVIDENCE_TOOL_PATHS,
        "modified": modified[:JOB_CHANGES_MAX_PATHS],
        "modified_count": len(modified),
        "read_count": len(rows) - len(modified),
    }


__all__ = ["EVIDENCE_NONE", "EVIDENCE_TOOL_PATHS", "job_changes"]
