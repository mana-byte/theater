"""RC10's frozen public contract, independent of daemon implementation."""

from __future__ import annotations

import ast
import socket
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, ValidationError

from theater.frontend.capabilities import (
    CALLBACK_CATALOG,
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
            "reason": "provider_unavailable",
        }
    )
    assert harness.installed and not harness.provider_ready and not harness.launch_available


def test_frontend_contract_has_no_private_imports() -> None:
    root = Path(__file__).parents[1] / "theater" / "frontend"
    forbidden = ("theater.daemon", "theater.client", "theater.config", "theater.tmux", "textual")
    imports: set[str] = set()
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                imports.add(node.module)
    assert not {name for name in imports if name.startswith(forbidden)}
