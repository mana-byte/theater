"""Bounded, read-only job facts for authenticated plugin sidecars.

Missing/GC: ``{handle, available: false}``. Available: ``handle``, ``available``,
``state``, ``error_code``, ``kind``, ``caller_id``, ``target_id``, ``created_at``,
``finished_at``, ``contract_preset``, ``structured_status``, ``delegate_status``,
``review_verdict``, ``git`` and ``handoff``. The last two are null or compact
``{pin_state, commit_sha, dirty_entries, clean}`` and ``{source_handle, task_kind}``.
Only a PLAYS_READ credential bound to a live plugin-dependent play also receives
``play: {instance_id, caller_seated_at_creation}``; an absent seat is false.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from theater.constants.response_contract import CONTRACT_PRESET_NAMES
from theater.daemon.persistence.repositories.mcp_plugins import McpPluginCredentialRecord
from theater.daemon.response_contracts.contract import StrictContract, parse_stored
from theater.daemon.schema import play_members
from theater.json_values import decode_finite_json
from theater.mcp_plugins.contracts import PluginCapability
from theater.models import BadRequest, Job

_MAX_HANDLES = 50
_MAX_HANDLE_CHARS = 256


async def facts(
    daemon: Any, params: dict[str, Any], record: McpPluginCredentialRecord
) -> list[dict]:
    """Read exact handles in input order; no participant, transcript, or Git lookup occurs."""
    handles = params.get("handles")
    if (
        not isinstance(handles, list)
        or len(handles) > _MAX_HANDLES
        or any(
            not isinstance(handle, str) or not handle.strip() or len(handle) > _MAX_HANDLE_CHARS
            for handle in handles
        )
    ):
        raise BadRequest(
            "jobs.facts requires at most 50 nonempty handles of at most 256 characters"
        )

    held = (
        daemon.plays.membership(record.participant_id)
        if PluginCapability.PLAYS_READ in record.grants
        else None
    )
    instance_id = (
        held.instance_id if held is not None and record.plugin_name in held.rules.plugins else None
    )
    result = []
    for handle in handles:
        job = daemon.store.get_job(handle)
        if job is None:
            result.append({"handle": handle, "available": False})
            continue
        result.append(_fact(daemon.store, job, instance_id))
    return result


def _fact(store: Any, job: Job, instance_id: str | None) -> dict:
    try:
        contract = parse_stored(job.response_format)
    except (TypeError, ValueError, RecursionError):
        contract = None
    preset = (
        contract.kind if contract is not None and contract.kind in CONTRACT_PRESET_NAMES else None
    )
    evidence = store.get_job_evidence(job.handle)
    gate = store.get_handoff_gate(job.handle)
    row: dict[str, Any] = {
        "handle": job.handle,
        "available": True,
        "state": job.state,
        "error_code": job.error_code,
        "kind": job.kind,
        "caller_id": job.caller_id,
        "target_id": job.target_id,
        "created_at": job.created_at,
        "finished_at": job.finished_at,
        "contract_preset": preset,
        "structured_status": job.structured_status,
        "delegate_status": None,
        "review_verdict": None,
        "git": None,
        "handoff": None,
    }
    if preset is not None and contract is not None:
        _parsed_preset(row, job, contract)
    if evidence is not None:
        git = evidence.git
        row["git"] = {
            "pin_state": git.pin_state,
            "commit_sha": git.commit_sha,
            "dirty_entries": git.dirty_entries,
            "clean": git.reviewable and git.dirty_entries == 0,
        }
    if gate is not None:
        row["handoff"] = {"source_handle": gate.source_handle, "task_kind": gate.task_kind}
    if instance_id is not None:
        seated = (
            store.conn.execute(
                select(play_members.c.participant_id)
                .where(play_members.c.instance_id == instance_id)
                .where(play_members.c.participant_id == job.caller_id)
                .where(play_members.c.created_at <= job.created_at)
            ).first()
            is not None
        )
        row["play"] = {
            "instance_id": instance_id,
            "caller_seated_at_creation": seated,
        }
    return row


def _parsed_preset(row: dict[str, Any], job: Job, contract: StrictContract) -> None:
    if (
        job.state != "done"
        or job.error_code is not None
        or job.structured_status != "parsed"
        or not contract.validate(job.structured_result).ok
    ):
        return
    valid, value = decode_finite_json(job.structured_result)
    if not valid or not isinstance(value, dict):
        return
    if contract.kind == "delegate":
        row["delegate_status"] = value.get("status")
    elif contract.kind == "review":
        row["review_verdict"] = value.get("verdict")
