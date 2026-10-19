"""Authenticated plugin job facts: bounded reads, stored evidence and caller play seats."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import delete

from theater import paths
from theater.daemon.handoff.models import GitFacts, HandoffGate, JobEvidence
from theater.daemon.plugins.credentials import mint_credential
from theater.daemon.response_contracts.contract import StrictContract, admit_schema
from theater.daemon.response_contracts.presets import PRESET_SCHEMAS
from theater.daemon.schema import jobs, play_members
from theater.mcp_plugins import MCP_PLUGIN_API_VERSION, PluginCapability
from theater.models import Job
from theater.plugin_client import PluginRemoteError, TheaterPluginClient
from theater.protocol import RemoteError
from theater.skills import PlayRules


def _credential(daemon, actor_id: str, *grants: PluginCapability, plugin: str = "acme") -> str:
    material = mint_credential()
    daemon.store.set_mcp_plugin_credential(
        actor_id,
        plugin_name=plugin,
        api_version=MCP_PLUGIN_API_VERSION,
        credential_id=material.credential_id,
        credential_verifier=material.verifier,
        grants=frozenset(grants),
        credential_path=str(paths.participant_plugin_dir(actor_id, plugin) / "credential"),
    )
    return material.credential


async def _facts(client, credential: str, handles: object, **extra) -> list[dict]:
    return await client.call(
        "plugin.call",
        credential=credential,
        operation="jobs.facts",
        params={"handles": handles, **extra},
    )


def _job(
    daemon,
    handle: str,
    *,
    caller_id: str | None = "caller",
    target_id: str | None = "target",
    created_at: float = 10,
    state: str = "done",
    response_format: str | None = None,
    structured_status: str | None = None,
    structured_result: str | None = None,
    error_code: str | None = None,
) -> None:
    daemon.store.create_job(
        Job(
            handle=handle,
            caller_id=caller_id,
            target_id=target_id,
            kind="send",
            prompt="SECRET_PROMPT",
            state=state,
            result="SECRET_RESULT",
            error_code=error_code,
            created_at=created_at,
            finished_at=created_at + 1 if state != "running" else None,
            response_format=response_format,
            structured_status=structured_status,
            structured_result=structured_result,
        )
    )


async def test_jobs_facts_authorizes_bounds_exact_order_and_missing_jobs(daemon, client):
    actor = daemon.registry.create_spawned(harness="fake", cwd="/tmp")
    denied = _credential(daemon, actor.id, PluginCapability.PLAYS_READ)
    with pytest.raises(RemoteError) as exc:
        await _facts(client, denied, ["exact.handle"])
    assert exc.value.code == "capability_denied"
    assert exc.value.details["required"] == "jobs.read"

    credential = _credential(daemon, actor.id, PluginCapability.JOBS_READ)
    _job(daemon, "exact.handle")
    _job(daemon, "gc-pruned")
    with daemon.store.write_unit() as unit:
        unit.connection.execute(delete(jobs).where(jobs.c.handle == "gc-pruned"))
    handles = ["missing", "exact.handle", "gc-pruned", "missing", "EXACT.HANDLE"]
    rows = await _facts(client, credential, handles)
    assert [row["handle"] for row in rows] == handles
    assert rows[0] == rows[3] == {"handle": "missing", "available": False}
    assert rows[2] == {"handle": "gc-pruned", "available": False}
    assert rows[4] == {"handle": "EXACT.HANDLE", "available": False}
    assert rows[1]["available"] is True
    assert "play" not in rows[1]
    assert await _facts(client, credential, []) == []
    assert len(await _facts(client, credential, ["absent"] * 50)) == 50

    for malformed in (None, "exact.handle", {}, [""], ["  "], [1], ["x" * 257], ["x"] * 51):
        with pytest.raises(RemoteError) as bad:
            await _facts(client, credential, malformed)
        assert bad.value.code == "bad_request"


async def test_jobs_facts_projects_only_valid_stored_preset_and_retained_evidence(daemon, client):
    actor = daemon.registry.create_spawned(harness="fake", cwd="/tmp")
    credential = _credential(daemon, actor.id, PluginCapability.JOBS_READ)
    review = StrictContract("review", admit_schema(PRESET_SCHEMAS["review"])).envelope()
    delegate = StrictContract("delegate", admit_schema(PRESET_SCHEMAS["delegate"])).envelope()
    lookalike = StrictContract("custom", admit_schema(PRESET_SCHEMAS["review"]), "review")
    answer = json.dumps(
        {
            "verdict": "approve",
            "reviewed": "abc..def",
            "summary": "fine",
            "findings": [{"severity": "nit", "issue": "SECRET_FINDING"}],
        }
    )
    cases = {
        "review": (review, "parsed", answer, "done", None),
        "custom": (lookalike.envelope(), "parsed", answer, "done", None),
        "hint": ('{"name":"review","verdict":"approve"}', "parsed", answer, "done", None),
        "malformed": (review, "parsed", '{"verdict":', "done", None),
        "no-output": (review, "parsed", None, "done", None),
        "unparsed": (review, "unable_to_parse", answer, "done", None),
        "failed": (review, "parsed", answer, "crashed", "failed"),
        "bad-envelope": (
            '{"$theater":"review","schema":{"default":NaN}}',
            "parsed",
            answer,
            "done",
            None,
        ),
        "delegate": (
            delegate,
            "parsed",
            '{"status":"done","summary":"SECRET_SUMMARY","blockers":[]}',
            "done",
            None,
        ),
    }
    for handle, (
        contract,
        structured_status,
        structured_result,
        state,
        error_code,
    ) in cases.items():
        _job(
            daemon,
            handle,
            response_format=contract,
            structured_status=structured_status,
            structured_result=structured_result,
            state=state,
            error_code=error_code,
        )

    review_evidence = JobEvidence(
        job_handle="review",
        outcome="done",
        error_code=None,
        answer="SECRET_EVIDENCE",
        answer_chars=15,
        answer_source="result",
        provenance="native",
        harness="fake",
        participant_id="target",
        workspace_id=None,
        cwd="/secret/path",
        git=GitFacts.from_status("a" * 40, "main", 0),
        captured_at=11,
    )
    daemon.store.put_job_evidence(review_evidence)
    daemon.store.put_job_evidence(
        replace(
            review_evidence,
            job_handle="malformed",
            git=GitFacts.from_status("b" * 40, "main", 2),
        )
    )
    daemon.store.put_job_evidence(
        replace(review_evidence, job_handle="no-output", git=GitFacts.unpinned("not_staged"))
    )
    with daemon.store.write_unit() as unit:
        daemon.store.create_handoff_gate(
            HandoffGate(
                job_handle="review",
                caller_id="caller",
                source_handle="source#1",
                source_participant_id="source",
                source_name="source",
                task_kind="independent_review",
                request="SECRET_REQUEST",
                state="opened",
                error_code=None,
                error=None,
                created_at=10,
                updated_at=11,
            ),
            connection=unit.connection,
        )
    rows = {row["handle"]: row for row in await _facts(client, credential, list(cases))}
    assert rows["review"]["contract_preset"] == "review"
    assert rows["review"]["review_verdict"] == "approve"
    assert rows["review"]["git"] == {
        "pin_state": "pinned",
        "commit_sha": "a" * 40,
        "dirty_entries": 0,
        "clean": True,
    }
    assert rows["review"]["handoff"] == {
        "source_handle": "source#1",
        "task_kind": "independent_review",
    }
    assert rows["malformed"]["git"] == {
        "pin_state": "dirty",
        "commit_sha": "b" * 40,
        "dirty_entries": 2,
        "clean": False,
    }
    assert rows["no-output"]["git"] == {
        "pin_state": "unpinned",
        "commit_sha": None,
        "dirty_entries": None,
        "clean": False,
    }
    assert rows["delegate"]["contract_preset"] == "delegate"
    assert rows["delegate"]["delegate_status"] == "done"
    assert rows["custom"]["contract_preset"] is None
    assert rows["hint"]["contract_preset"] is None
    for handle in (
        "custom",
        "hint",
        "malformed",
        "no-output",
        "unparsed",
        "failed",
        "bad-envelope",
    ):
        assert rows[handle]["review_verdict"] is None
    assert rows["bad-envelope"]["contract_preset"] is None
    assert rows["failed"]["error_code"] == "failed"
    assert rows["custom"]["structured_status"] == "parsed"
    assert rows["custom"]["git"] is None and rows["custom"]["handoff"] is None
    encoded = json.dumps(rows)
    for secret in (
        "SECRET_PROMPT",
        "SECRET_RESULT",
        "SECRET_FINDING",
        "SECRET_SUMMARY",
        "SECRET_EVIDENCE",
        "SECRET_REQUEST",
        "/secret/path",
    ):
        assert secret not in encoded


async def test_jobs_facts_play_proof_requires_current_binding_and_caller_seat_at_issue(
    daemon, client
):
    master = daemon.registry.create_spawned(harness="fake", cwd="/tmp")
    actor = daemon.registry.create_spawned(harness="fake", cwd="/tmp")
    caller = daemon.registry.create_spawned(harness="fake", cwd="/tmp")
    late = daemon.registry.create_spawned(harness="fake", cwd="/tmp")
    target = daemon.registry.create_spawned(harness="fake", cwd="/tmp")
    outsider = daemon.registry.create_spawned(harness="fake", cwd="/tmp")
    foreign_master = daemon.registry.create_spawned(harness="fake", cwd="/tmp")
    rules = PlayRules(
        play="fixture",
        master="master",
        positions={"master": ("member",), "member": ()},
        roles={},
        plugins=("acme",),
    )
    with daemon.store.write_unit() as unit:
        instance = daemon.store.plays.start(
            master.id, rules, created_at=1, connection=unit.connection
        )
        for participant in (caller, target):
            daemon.store.plays.add_member(
                participant.id, instance, "member", created_at=2, connection=unit.connection
            )
        for participant in (actor, late):
            daemon.store.plays.add_member(
                participant.id, instance, "member", created_at=20, connection=unit.connection
            )
        other_instance = daemon.store.plays.start(
            foreign_master.id, rules, created_at=1, connection=unit.connection
        )
        daemon.store.plays.add_member(
            outsider.id, other_instance, "member", created_at=2, connection=unit.connection
        )
    proof = _credential(daemon, actor.id, PluginCapability.JOBS_READ, PluginCapability.PLAYS_READ)
    no_play_grant = _credential(daemon, late.id, PluginCapability.JOBS_READ)
    wrong_plugin = _credential(
        daemon, outsider.id, PluginCapability.JOBS_READ, PluginCapability.PLAYS_READ, plugin="other"
    )
    _job(daemon, "seated", caller_id=caller.id, target_id=target.id)
    _job(daemon, "late", caller_id=late.id, target_id=target.id)
    _job(daemon, "foreign", caller_id=outsider.id, target_id=target.id)
    _job(daemon, "target-only", caller_id="unseated", target_id=target.id)
    _job(daemon, "actor", caller_id=actor.id, target_id=target.id)
    daemon.registry.mark_dead(caller.id)

    handles = ["seated", "late", "foreign", "target-only", "actor", "missing"]
    rows = await _facts(
        client,
        proof,
        handles,
        caller_id=outsider.id,
        actor_id=outsider.id,
        instance_id=other_instance,
        grants=["plays.read"],
        plugin_name="other",
    )
    assert [row["play"]["caller_seated_at_creation"] for row in rows[:-1]] == [
        True,
        False,
        False,
        False,
        False,
    ]
    assert all(row["play"]["instance_id"] == instance for row in rows[:-1])
    assert rows[-1] == {"handle": "missing", "available": False}
    assert "play" not in (await _facts(client, no_play_grant, ["seated"], grants=["plays.read"]))[0]
    assert "play" not in (await _facts(client, wrong_plugin, ["seated"]))[0]

    with daemon.store.write_unit() as unit:
        unit.connection.execute(
            delete(play_members).where(
                play_members.c.participant_id == caller.id,
                play_members.c.instance_id == instance,
            )
        )
    refreshed = (await _facts(client, proof, ["seated"]))[0]
    assert refreshed["available"] is True
    assert refreshed["play"] == {
        "instance_id": instance,
        "caller_seated_at_creation": False,
    }
    assert (await _facts(client, no_play_grant, ["seated"]))[0]["available"] is True


def test_public_client_forwards_exact_handles_and_preserves_remote_errors(tmp_path: Path):
    credential_path = tmp_path / "credential"
    material = mint_credential()
    credential_path.write_text(material.credential + "\n")

    class Client:
        def __init__(self):
            self.calls = []

        async def call(self, method, **params):
            self.calls.append((method, params))
            if len(self.calls) == 2:
                raise RemoteError("bad_request", "invalid handles")
            return [{"handle": "x", "available": False}]

    raw = Client()
    typed = TheaterPluginClient(credential_path=credential_path, client=raw)
    assert asyncio.run(typed.get_job_facts(["x", "x"])) == [{"handle": "x", "available": False}]
    assert raw.calls[0] == (
        "plugin.call",
        {
            "credential": material.credential,
            "operation": "jobs.facts",
            "params": {"handles": ["x", "x"]},
        },
    )
    with pytest.raises(PluginRemoteError) as error:
        asyncio.run(typed.get_job_facts(["bad"]))
    assert error.value.code == "bad_request"
