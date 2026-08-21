"""Administrative RPC handlers: ping, gc, shutdown."""

from __future__ import annotations

from theater import paths, protocol
from theater.daemon.rpc.router import method


@method("ping")
async def _ping(daemon, params: dict) -> dict:
    return {"pong": True, "protocol": protocol.PROTOCOL_VERSION}


@method("gc")
async def _gc(daemon, params: dict) -> dict:
    """Run a garbage-collection sweep on demand and report what it did.

    The automatic ``_gc_loop`` runs ``sweep`` every ``retention.interval``
    seconds; this method is for a user who wants it *now*, or who wants to
    reclaim disk space with ``--vacuum``.

    Deleting rows does not shrink the database file — measured, deleting 94%
    of the bus table left the file the same size (it grew, because of the
    WAL). Only ``VACUUM`` reclaims space, by rewriting the whole file under
    an exclusive lock. So the response carries before/after byte sizes so the
    caller can report what was actually reclaimed, and a ``vacuum_ran`` flag.
    """
    from theater.daemon.gc import sweep, vacuum
    from theater.daemon.rpc.usage import _retention_floor

    db_path = paths.db_path()
    db_bytes_before = db_path.stat().st_size if db_path.exists() else 0

    result = await sweep(
        daemon.store,
        daemon.config.retention,
        live_handles=frozenset(daemon.jobs._events),
    )

    vacuum_ran = bool(params.get("vacuum", False))
    if vacuum_ran:
        vacuum(daemon.store)

    db_bytes_after = db_path.stat().st_size if db_path.exists() else 0

    return {
        "bus": result.bus,
        "jobs": result.jobs,
        "touch": result.touch,
        "participants": result.participants,
        "running_marked": result.running_marked,
        "scratchpad": result.scratchpad,
        "coverage": _retention_floor(daemon),
        "db_bytes_before": db_bytes_before,
        "db_bytes_after": db_bytes_after,
        "vacuum_ran": vacuum_ran,
    }


@method("shutdown")
async def _shutdown(daemon, params: dict) -> dict:
    daemon.stop()
    return {"stopping": True}
