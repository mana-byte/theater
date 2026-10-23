"""Recall timeline and segment read operations."""

from __future__ import annotations

from pathlib import Path

from theater.daemon import workers
from theater.daemon.request_values import integer_param, require
from theater.models import BadRequest


async def recall(daemon, params: dict) -> dict:
    """Per-file timelines of what Theater watched happen.

    Gap detection is pure SQL; two subprocesses per query regardless of path count
    (see ``theater.daemon.recall`` for the budget).
    """
    from theater.daemon.recall import _dirty_set, _git_root, hash_current_files
    from theater.daemon.recall import recall as _do_recall

    paths = require(params, "paths")
    if not isinstance(paths, list) or not paths:
        raise BadRequest("paths must be a non-empty list")
    depth = integer_param(params.get("depth", 5), "depth", method_name="recall")
    caller_cwd = params.get("caller_cwd")
    # Pre-fetch git calls off the event loop, then pass them in so recall() forks nothing.
    effective_cwd = caller_cwd or str(Path.cwd())
    precomputed_root = await workers.to_thread(_git_root, effective_cwd, label="recall.git_root")
    precomputed_dirty = await workers.to_thread(_dirty_set, effective_cwd, label="recall.dirty_set")
    try:
        precomputed_current = await workers.to_thread(
            hash_current_files,
            precomputed_root or effective_cwd,
            paths,
            label="recall.current_hashes",
        )
    except ValueError as exc:
        raise BadRequest(str(exc)) from exc
    result = _do_recall(
        daemon.store,
        paths=paths,
        depth=depth,
        caller_cwd=caller_cwd,
        precomputed_root=precomputed_root,
        precomputed_dirty=precomputed_dirty,
        precomputed_current=precomputed_current,
    )
    _attach_parent_names(daemon, result)
    return result


def _attach_parent_names(daemon, result: dict) -> None:
    """Names come from the live registry; historical identities keep their stable ids."""
    for entry in result.values():
        for point in entry.get("timeline", []):
            for id_key, name_key in (("parent_id", "parent_name"), ("participant_id", "name")):
                participant_id = point.get(id_key)
                if participant_id is None:
                    continue
                try:
                    point[name_key] = daemon.registry.get(participant_id).name
                except Exception:
                    point[name_key] = None


async def recall_read(daemon, params: dict) -> dict:
    """Explain one point of a recall timeline.

    Separate call because a gap segment is the feature's only ``git log`` fork; job segments
    read through ``open_source`` so database-backed transcripts work too.
    """
    from theater.daemon.recall_read import read_segment

    segment_id = require(params, "segment_id")
    caller_cwd = params.get("caller_cwd") or "."
    return await read_segment(
        segment_id,
        store=daemon.store,
        registry=daemon.registry,
        cwd=caller_cwd,
        observer=daemon.observer,
    )
