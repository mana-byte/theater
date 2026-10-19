"""The frozen public contract: catalog, schemas and the validator cache."""

from __future__ import annotations

import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from jsonschema import Draft202012Validator, ValidationError

from theater.frontend.capabilities import (
    CALLBACK_CATALOG,
    MAX_ERROR_CODE_CHARS,
    MAX_ERROR_MESSAGE_CHARS,
    MAX_EXACT_JSON_INTEGER,
    METHOD_CATALOG,
    PUBLIC_API_MAJOR,
    PUBLIC_API_MINOR,
    ConnectionRole,
    MethodClass,
)
from theater.frontend.dto import (
    EVENT_KINDS,
    Actor,
    Event,
    EventTransaction,
    HarnessCatalogEntry,
    Job,
    Operation,
    Response,
)
from theater.frontend.errors import ErrorCode, ErrorValue
from theater.frontend.schemas import (
    RESOURCE_NAMES,
    catalog,
    load_schema_resources,
    validate_callback_request,
    validate_callback_response,
    validate_public_request,
    validate_public_response,
    validator_for,
)

EXPECTED_METHODS = {
    "frontend.handshake",
    "frontend.contract.get",
    "frontend.schemas.get",
    "frontend.health.get",
    "frontend.state.snapshot",
    "frontend.state.page",
    "frontend.state.release",
    "frontend.state.follow",
    "frontend.participants.list",
    "frontend.participants.get",
    "frontend.participants.tree",
    "frontend.participants.resume_candidates",
    "frontend.participants.spawn",
    "frontend.participants.adopt",
    "frontend.participants.update",
    "frontend.participants.status",
    "frontend.participants.terminate",
    "frontend.participants.transfer_control",
    "frontend.controls.get",
    "frontend.controls.send",
    "frontend.controls.steer",
    "frontend.controls.queue_followup",
    "frontend.controls.interrupt",
    "frontend.controls.settings.update",
    "frontend.operations.list",
    "frontend.operations.get",
    "frontend.operations.await",
    "frontend.operations.reconcile",
    "frontend.jobs.list",
    "frontend.jobs.get",
    "frontend.jobs.await",
    "frontend.providers.register",
    "frontend.providers.update",
    "frontend.providers.list",
    "frontend.providers.get",
    "frontend.providers.heartbeat",
    "frontend.providers.report",
    "frontend.providers.terminals.list",
    "frontend.providers.terminals.inspect",
    "frontend.workspaces.register",
    "frontend.workspaces.list",
    "frontend.workspaces.get",
    "frontend.workspaces.cleanup",
    "frontend.workspaces.prepare_delete",
    "frontend.workspaces.confirm_delete",
    "frontend.workspaces.cancel_delete",
    "frontend.scratchpad.namespaces",
    "frontend.scratchpad.get",
    "frontend.scratchpad.write",
    "frontend.scratchpad.delete",
    "frontend.catalogs.harnesses",
    "frontend.catalogs.models",
    "frontend.skills.list",
    "frontend.skills.load",
    "frontend.transcripts.read",
    "frontend.transcripts.candidates",
    "frontend.transcripts.bind",
    "frontend.recall.query",
    "frontend.recall.read",
    "frontend.trajectory.snapshot",
    "frontend.trajectory.follow",
    "frontend.trajectory.close",
    "frontend.trajectory.locate",
    "frontend.trajectory.search",
    "frontend.usage.totals",
    "frontend.usage.summary",
    "frontend.usage.by_harness",
    "frontend.usage.by_participant",
    "frontend.stats.get",
    "frontend.bus.tail",
}


EXPECTED_CALLBACKS = {
    "terminal.create",
    "terminal.inventory",
    "terminal.inspect",
    "terminal.deliver",
    "terminal.interrupt",
    "terminal.terminate",
}


def test_complete_catalog_and_offline_schema_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    assert set(METHOD_CATALOG) == EXPECTED_METHODS
    assert set(CALLBACK_CATALOG) == EXPECTED_CALLBACKS
    assert len(RESOURCE_NAMES) == len(load_schema_resources())
    forbidden = {"frontend.rpc.call", "frontend.plugin.call", "frontend.gc", "frontend.shutdown"}
    assert not forbidden & set(METHOD_CATALOG)

    def no_network(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("schema resolution attempted network access")

    monkeypatch.setattr(socket, "create_connection", no_network)
    for schema in load_schema_resources().values():
        Draft202012Validator.check_schema(dict(schema))
    for spec in (*METHOD_CATALOG.values(), *CALLBACK_CATALOG.values()):
        validator_for(spec.request_schema_id)
        validator_for(spec.response_schema_id)
        validator_for(spec.params_schema_id)
        validator_for(spec.result_schema_id)

    resources = load_schema_resources()
    schema_base = "https://theater.dev/schemas/frontend/1.0/"
    methods = resources[f"{schema_base}methods.json"]["$defs"]
    callbacks = resources[f"{schema_base}callbacks.json"]["$defs"]
    common = resources[f"{schema_base}common.json"]["$defs"]
    errors = resources[f"{schema_base}errors.json"]["$defs"]
    assert set(methods["methodName"]["enum"]) == EXPECTED_METHODS - {"frontend.handshake"}
    assert set(callbacks["callbackMethod"]["enum"]) == EXPECTED_CALLBACKS
    assert tuple(common["event"]["properties"]["kind"]["enum"]) == EVENT_KINDS
    assert set(errors["knownCode"]["enum"]) == {code.value for code in ErrorCode}
    assert common["identifier"]["maxLength"] == MAX_ERROR_CODE_CHARS
    assert common["error"]["properties"]["message"]["maxLength"] == MAX_ERROR_MESSAGE_CHARS

    business = {MethodClass.WRITE, MethodClass.OPERATION}
    assert all(
        spec.idempotency_required == (spec.method_class in business)
        for spec in METHOD_CATALOG.values()
    )
    assert METHOD_CATALOG["frontend.providers.report"].roles == frozenset({ConnectionRole.PROVIDER})
    required_errors = {
        "handshake_required",
        "incompatible_api",
        "missing_capability",
        "wrong_connection_role",
        "idempotency_conflict",
        "provider_unavailable",
        "provider_busy",
        "stale_generation",
        "terminal_identity_mismatch",
        "ownership_conflict",
        "workspace_in_use",
        "workspace_deleting",
        "snapshot_expired",
        "resnapshot_required",
    }
    assert required_errors <= {code.value for code in ErrorCode}


def test_json_value_single_pass_preserves_recursive_limits() -> None:
    validator = validator_for(
        "https://theater.dev/schemas/frontend/1.0/common.json#/$defs/jsonValue"
    )
    validator.validate({"values": [None, True, False, 1, 1.5, "text", [], {}]})
    for value in ([None] * 500, {str(i): None for i in range(2048)}, "x" * 1048576, {"x" * 512: 1}):
        validator.validate({"nested": value})
    for value in (
        [None] * 501,
        {str(i): None for i in range(2049)},
        "x" * 1048577,
        {"x" * 513: 1},
        {1, 2},
    ):
        with pytest.raises(ValidationError):
            validator.validate({"nested": [value]})
    for value in (float("nan"), float("inf"), -float("inf")):
        with pytest.raises(ValidationError):
            validate_public_response(
                "frontend.trajectory.snapshot", {"id": 1, "ok": True, "result": {"nested": [value]}}
            )


def test_representative_public_envelopes_and_strict_requests() -> None:
    handshake = {
        "id": 1,
        "method": "frontend.handshake",
        "params": {
            "api": {"major": PUBLIC_API_MAJOR, "minor": PUBLIC_API_MINOR},
            "client_id": "regie-local-a",
            "role": "operator",
            "channel": "rpc",
            "required_capabilities": ["orchestration.v1", "state.follow.v1"],
        },
    }
    validate_public_request(handshake)
    validate_public_request(
        {
            "id": 2,
            "method": "frontend.controls.send",
            "idempotency_key": "send-a-0007",
            "params": {"participant_id": "participant-a", "prompt": "Review this."},
        }
    )
    validate_public_response(
        "frontend.controls.send",
        {
            "id": 2,
            "ok": True,
            "result": {
                "operation_id": "operation-a",
                "state": "accepted",
                "participant_id": "participant-a",
                "job_handle": "job-a",
                "future_field": True,
            },
        },
    )
    validate_public_response(
        "frontend.controls.send",
        {
            "id": 2,
            "ok": False,
            "error": {
                "code": "provider_unavailable",
                "message": "The selected provider is offline.",
                "details": {"provider_id": "provider-a"},
            },
        },
    )

    invalid = [
        {**handshake, "id": 0},
        {**handshake, "id": True},
        {**handshake, "id": MAX_EXACT_JSON_INTEGER + 1},
        {**handshake, "params": {**handshake["params"], "role": "administrator"}},
        {**handshake, "params": {**handshake["params"], "unexpected": True}},
    ]
    for value in invalid:
        with pytest.raises(ValidationError):
            validate_public_request(value)
    with pytest.raises(ValueError, match="required"):
        validate_public_request(
            {
                "id": 2,
                "method": "frontend.controls.send",
                "params": {"participant_id": "participant-a", "prompt": "Review this."},
            }
        )
    with pytest.raises(ValidationError):
        validate_public_request(
            {
                "id": 3,
                "method": "frontend.participants.get",
                "params": {"participant_id": "participant-a", "unexpected": True},
            }
        )
    with pytest.raises(ValidationError, match="non-finite"):
        validate_public_request(
            {
                "id": 3,
                "method": "frontend.operations.await",
                "params": {"operation_id": "operation-a", "wait_seconds": float("nan")},
            }
        )


def test_transcript_and_resume_results_use_domain_schemas() -> None:
    identity = {
        "state": "trusted",
        "session_id": "session-a",
        "provenance": "exact",
        "location": "/tmp/transcript.jsonl",
        "domain": None,
        "detail": None,
    }
    validate_public_response(
        "frontend.participants.resume_candidates",
        {
            "id": 1,
            "ok": True,
            "result": {
                "items": [
                    {
                        "participant_id": "participant-a",
                        "harness": "codex",
                        "resume_state": "resumable",
                        "transcript_identity": identity,
                    }
                ],
                "next_cursor": None,
            },
        },
    )
    validate_public_response(
        "frontend.transcripts.read",
        {
            "id": 2,
            "ok": True,
            "result": {
                "id": "participant-a",
                "events": [
                    {
                        "event_position": 0,
                        "index": 1,
                        "role": "assistant",
                        "text": "done",
                        "tool_name": None,
                        "turn_end": True,
                        "turn_terminal": True,
                        "text_start_byte": 0,
                        "text_end_byte": 4,
                        "text_total_bytes": 4,
                        "reaches_text_start": True,
                    }
                ],
                "path": "/tmp/transcript.jsonl",
                "cursor": None,
                "next_cursor": None,
                "has_more": False,
                "truncated": False,
            },
        },
    )
    validate_public_response(
        "frontend.transcripts.candidates",
        {
            "id": 3,
            "ok": True,
            "result": {
                "items": [
                    {
                        "location": "/tmp/transcript.jsonl",
                        "session_id": "session-a",
                        "mtime": 1.0,
                        "size": 10,
                        "provenance": "exact",
                        "rejection_reason": None,
                        "domain": None,
                        "owner": None,
                        "tombstone": None,
                    }
                ],
                "next_cursor": None,
            },
        },
    )
    validate_public_response(
        "frontend.transcripts.bind",
        {
            "id": 4,
            "ok": True,
            "result": {
                "participant_id": "participant-a",
                "location": "/tmp/transcript.jsonl",
                "session_id": "session-a",
                "prior_owner_id": None,
            },
        },
    )

    malformed = {
        "id": 5,
        "ok": True,
        "result": {"items": [{"participant_id": "participant-a"}]},
    }
    with pytest.raises(ValidationError):
        validate_public_response("frontend.participants.resume_candidates", malformed)
    with pytest.raises(ValidationError):
        validate_public_response("frontend.transcripts.candidates", malformed)


def test_callback_and_event_transaction_frames() -> None:
    callback = {
        "type": "request",
        "id": "callback-a",
        "method": "terminal.deliver",
        "params": {
            "operation_id": "operation-a",
            "provider_generation": 4,
            "participant_id": "participant-a",
            "terminal_id": "terminal-a",
            "terminal_incarnation": "incarnation-a",
            "expected_occupant": "occupant-a",
            "action": {"kind": "submit_text", "text": "Review this."},
            "require_absent": True,
        },
    }
    validate_callback_request(callback)
    validate_callback_response(
        "terminal.deliver",
        {
            "type": "response",
            "id": "callback-a",
            "result": {
                "operation_id": "operation-a",
                "provider_generation": 4,
                "terminal_id": "terminal-a",
                "terminal_incarnation": "incarnation-a",
                "delivery": "accepted",
                "presence_revision": 19,
            },
        },
    )
    with pytest.raises(ValidationError):
        validate_callback_response(
            "terminal.deliver",
            {
                "type": "response",
                "id": "callback-a",
                "result": {
                    "operation_id": "operation-a",
                    "provider_generation": 4,
                    "terminal_id": "terminal-a",
                    "terminal_incarnation": "incarnation-a",
                    "delivery": "completed",
                },
            },
        )
    with pytest.raises(ValidationError):
        validate_callback_request(
            {**callback, "params": {**callback["params"], "unexpected": True}}
        )

    transaction = {
        "transaction_id": "transaction-a",
        "events": [
            {
                "kind": "participant.updated",
                "entity_id": "participant-a",
                "entity_revision": 7,
                "payload": {"status": "working"},
            }
        ],
        "ending_cursor": {"stream_id": "stream-a", "sequence": 31},
    }
    validator_for(
        "https://theater.dev/schemas/frontend/1.0/events.json#/$defs/transaction"
    ).validate(transaction)
    assert EventTransaction.from_wire(transaction).ending_cursor.sequence == 31


def test_unknown_response_values_and_fields_are_preserved() -> None:
    response = Response.from_wire(
        {"id": 7, "ok": True, "result": {"state": "future"}, "future_envelope": "kept"}
    )
    assert response.extra["future_envelope"] == "kept"

    operation = Operation.from_wire(
        {
            "operation_id": "operation-a",
            "kind": "future-action",
            "state": "paused-by-provider",
            "phase": "future-phase",
            "actor": Actor("client-a").to_wire(),
            "target_ids": ["participant-a"],
            "future_field": {"nested": [1, 2]},
        }
    )
    assert operation.known_state is None
    assert operation.extra["future_field"] == {"nested": (1, 2)}

    event = Event.from_wire(
        {
            "kind": "future.entity_changed",
            "entity_id": "entity-a",
            "entity_revision": 8,
            "payload": {},
            "future_field": "kept",
        }
    )
    assert not event.known_kind
    assert event.extra["future_field"] == "kept"

    error = ErrorValue.from_wire(
        {
            "code": "future_refusal",
            "message": "A newer server refused the request.",
            "details": {"revision": 9},
            "future_field": True,
        }
    )
    assert error.known_code is None
    assert error.extra["future_field"] is True

    historical_job = Job.from_wire(
        {
            "handle": "job-old",
            "state": "done",
            "kind": "send",
            "legacy_caller_id": "participant-old",
            "future_field": "kept",
        }
    )
    assert historical_job.actor is None
    assert historical_job.legacy_caller_id == "participant-old"
    assert historical_job.extra["future_field"] == "kept"

    harness = HarnessCatalogEntry.from_wire(
        {
            "name": "codex",
            "installed": True,
            "compatible": True,
            "supported_wiring": ["mcp", "hooks"],
            "requires_terminal": True,
            "provider_ready": False,
            "launch_available": False,
            "approvals": ["manual", "edits", "yolo"],
            "binary": "codex",
            "binaries": ["codex-wrapper"],
            "reason": "provider_unavailable",
        }
    )
    assert harness.installed and not harness.provider_ready and not harness.launch_available
    assert harness.approvals == ("manual", "edits", "yolo")
    assert harness.binary == "codex"
    assert harness.binaries == ("codex-wrapper",)
    assert harness.to_wire()["approvals"] == ["manual", "edits", "yolo"]


def test_usage_by_participant_has_strict_params_and_result_schemas() -> None:
    spec = METHOD_CATALOG["frontend.usage.by_participant"]
    validator_for(spec.params_schema_id).validate(
        {"since": 10.0, "participant_ids": ["participant-a", "participant-b"], "limit": 2}
    )
    result = {
        "since": 10.0,
        "truncated": False,
        "participants": [
            {
                "participant_id": "participant-a",
                "harness": "codex",
                "models": ["gpt-5"],
                "input_tokens": 12,
                "output_tokens": 4,
                "cache_creation_input_tokens": 3,
                "cache_read_input_tokens": 2,
                "reasoning_output_tokens": 1,
                "cost_microcents": 50,
                "first_at": 10.0,
                "last_at": 20.0,
            }
        ],
    }
    validator_for(spec.result_schema_id).validate(result)
    with pytest.raises(ValidationError):
        validator_for(spec.params_schema_id).validate({"participant_ids": ["participant-a"] * 2})
    with pytest.raises(ValidationError):
        validator_for(spec.params_schema_id).validate({"limit": 501})
    with pytest.raises(ValidationError):
        validator_for(spec.result_schema_id).validate(
            {**result, "participants": [{**result["participants"][0], "cost_microcents": -1}]}
        )


@pytest.fixture
def fresh_validator_cache():
    catalog.schema_registry()  # warm resource loading so its check_schema calls never count
    catalog._validators.clear()
    yield
    catalog._validators.clear()


@pytest.fixture
def check_calls(monkeypatch):
    calls: list[object] = []
    real = Draft202012Validator.check_schema

    def spy(schema, *args, **kwargs):
        calls.append(schema)
        return real(schema, *args, **kwargs)

    monkeypatch.setattr(Draft202012Validator, "check_schema", staticmethod(spy))
    return calls


@pytest.mark.usefixtures("fresh_validator_cache")
def test_cached_instance_still_validates_every_payload():
    schema_id = METHOD_CATALOG["frontend.health.get"].params_schema_id
    first = catalog.validator_for(schema_id)
    assert catalog.validator_for(schema_id) is first
    first.validate({})
    with pytest.raises(ValidationError):
        first.validate({"unexpected": 1})
    first.validate({})


@pytest.mark.usefixtures("fresh_validator_cache")
def test_each_schema_checked_once(check_calls):
    ids = {m.params_schema_id for m in list(METHOD_CATALOG.values())[:5]}
    for _ in range(20):
        for schema_id in ids:
            catalog.validator_for(schema_id)
    assert len(check_calls) == len(ids)


@pytest.mark.usefixtures("fresh_validator_cache")
def test_concurrent_calls_construct_once(monkeypatch, check_calls):
    schema_id = METHOD_CATALOG["frontend.health.get"].params_schema_id
    constructions: list[int] = []

    def counting(*args, **kwargs):
        constructions.append(1)
        if len(constructions) == 1:
            time.sleep(0.2)  # a racy cache would let other missing threads overlap here
        return Draft202012Validator(*args, **kwargs)

    monkeypatch.setattr(catalog, "Draft202012Validator", counting)
    workers = 16
    barrier = threading.Barrier(workers)

    def call(_):
        barrier.wait()
        return catalog.validator_for(schema_id)

    with ThreadPoolExecutor(workers) as pool:
        got = list(pool.map(call, range(workers)))
    assert all(v is got[0] for v in got)
    assert len(constructions) == 1
    assert len(check_calls) == 1


@pytest.mark.usefixtures("fresh_validator_cache")
def test_cache_is_bounded(monkeypatch):
    monkeypatch.setattr(catalog, "_VALIDATOR_CACHE_MAX", 3)
    for i in range(10):
        catalog.validator_for(f"{catalog.SCHEMA_BASE_URI}common.json#/$defs/x{i}")
    assert len(catalog._validators) == 3
