"""``config.reload``: take the daemon's own config file's hot policy settings without restarting."""

from __future__ import annotations

import logging

from theater import config
from theater.config.reload import compare
from theater.daemon.rpc.router import method
from theater.models import BadRequest

logger = logging.getLogger("theater.daemon")


@method("config.reload")
async def _config_reload(daemon, params: dict) -> dict:
    """Parse, compare and swap in one step; a single restart-only difference refuses the file.

    Each policy check reads ``daemon.config`` when it runs, so the cutover is per check: work
    already past a check keeps its outcome. No registry, plugin or service is touched.
    """
    dry_run = params.get("dry_run", False)
    if not isinstance(dry_run, bool):
        raise BadRequest("config.reload parameter 'dry_run' must be a boolean")
    try:
        candidate = config.load()
    except config.ConfigError as exc:
        raise BadRequest(f"{exc} — the running daemon kept its current settings") from exc
    changes = compare(daemon.config, candidate)
    applied = not dry_run and not changes.restart and changes.any
    if applied:
        daemon.config = candidate
        logger.info(
            "config reloaded from %s: %s",
            candidate.path,
            ", ".join((*changes.hot, *changes.outside_daemon)),
        )
    elif changes.restart and not dry_run:
        logger.info("config reload refused; restart-only: %s", ", ".join(changes.restart))
    return {
        "path": str(candidate.path),
        "dry_run": dry_run,
        "applied": applied,
        "changed": list(changes.hot),
        "restart_required": list(changes.restart),
        "outside_daemon": list(changes.outside_daemon),
    }
