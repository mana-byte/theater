"""Codex native runtime offline conformance: retained bundles, schemas and index."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.native.codex_native_client import (
    expected_accept,
)
from tests.native.codex_qualify_runtime import validate_bundle_index
from tests.support.paths import fixture
from tests.support.tables import run_rows
from theater.harness.builtin.plugins.codex.runtime_plan import (
    CODEX_RUNTIME_QUALIFIED_FLOOR,
    codex_version_refusal,
    parse_codex_version,
)

FIXTURES = fixture("codex_native_runtime")


#: Every behaviour file a complete qualified bundle must carry.
BUNDLE_BEHAVIOR_FILES = (
    "installed_release.json",
    "handshake.json",
    "thread_lifecycle.json",
    "turn_control.json",
    "approval.json",
    "capabilities.json",
    "unsupported_capabilities.json",
    "ui_topology.json",
)


#: Every generated schema file a complete qualified bundle must carry.
BUNDLE_SCHEMA_FILES = (
    "ClientRequest.json",
    "ClientNotification.json",
    "ServerRequest.json",
    "ServerNotification.json",
    "JSONRPCRequest.json",
    "JSONRPCMessage.json",
)


CANDIDATES_DIRNAME = "candidates"


#: The only committed bundles: the floor release's and a recent qualified release's.
RETAINED_BUNDLES = ("floor", "latest")


def load_index() -> dict:
    index = json.loads((FIXTURES / "index.json").read_text())
    validate_bundle_index(index, fixture_root=FIXTURES)
    return index


def bundle_dir(role: str) -> Path:
    """Resolve one retained bundle from the explicit index, never by version."""
    bundles = load_index()["bundles"]
    assert role in bundles, f"{role} has no index entry; the index is the only selector"
    return FIXTURES / bundles[role]["directory"]


def load_fixture(role: str, name: str) -> dict:
    return json.loads((bundle_dir(role) / name).read_text())


def load_schema(role: str, name: str) -> dict:
    return json.loads((bundle_dir(role) / "protocol_schema" / name).read_text())


REQUIRED_CLIENT_METHODS = (
    "initialize",
    "thread/start",
    "thread/resume",
    "thread/fork",
    "turn/start",
    "turn/steer",
    "turn/interrupt",
    "thread/settings/update",
    "thread/queue/add",
)


REQUIRED_APPROVAL_METHODS = (
    "item/commandExecution/requestApproval",
    "item/fileChange/requestApproval",
    "execCommandApproval",
    "applyPatchApproval",
)


REQUIRED_NOTIFICATIONS = (
    "turn/started",
    "turn/completed",
    "thread/status/changed",
    "item/completed",
    "item/agentMessage/delta",
)


def schema_methods(schema: dict) -> list[str]:
    methods: list[str] = []
    for variant in schema.get("oneOf", []):
        enum = variant.get("properties", {}).get("method", {}).get("enum")
        if enum:
            methods.append(enum[0])
    return methods


def _params_of(schema: dict, method: str) -> dict:
    for variant in schema["oneOf"]:
        if variant["properties"]["method"]["enum"] == [method]:
            return variant["properties"]["params"]
    raise AssertionError(f"{method} not found")


@pytest.mark.parametrize("role", RETAINED_BUNDLES)
class TestOfflineConformanceBundle:
    """One item pinning every offline conformance rule of a retained release bundle.

    Each labelled rule below is a separate check run in turn; review the list
    when editing so no rule is silently dropped.
    """

    def test_offline_conformance_manifest(self, role: str) -> None:  # noqa: PLR0915 — a deliberate manifest
        def bundle(name: str) -> dict:
            return load_fixture(role, name)

        def wire(name: str) -> dict:
            return load_schema(role, name)

        def installed_release_facts() -> None:
            facts = bundle("installed_release.json")
            version = parse_codex_version(facts["installed_version_output"])
            assert version is not None
            assert facts["installed_version"] == f"codex-cli {version}"
            # The floor bundle is the floor release; any other must still be inside the gate.
            if role == "floor":
                assert version == ".".join(map(str, CODEX_RUNTIME_QUALIFIED_FLOOR))
            assert codex_version_refusal(version) is None, "retained bundle is outside the gate"
            assert facts["schema_generation"]["command"].startswith("codex app-server")

        def required_client_methods_present() -> None:
            methods = schema_methods(wire("ClientRequest.json"))
            for method in REQUIRED_CLIENT_METHODS:
                assert method in methods, f"{method} missing from installed-release schema"

        def required_approval_methods_present() -> None:
            methods = schema_methods(wire("ServerRequest.json"))
            for method in REQUIRED_APPROVAL_METHODS:
                assert method in methods, f"{method} missing from installed-release schema"

        def required_notifications_present() -> None:
            methods = schema_methods(wire("ServerNotification.json"))
            for method in REQUIRED_NOTIFICATIONS:
                assert method in methods, f"{method} missing from installed-release schema"

        def wire_messages_omit_jsonrpc_version() -> None:
            request = wire("JSONRPCRequest.json")
            assert "jsonrpc" not in request["properties"]
            assert set(request["required"]) == {"id", "method"}
            message = wire("JSONRPCMessage.json")
            for variant in message["anyOf"]:
                definition = message["definitions"][variant["$ref"].split("/")[-1]]
                assert "jsonrpc" not in definition["properties"]
            assert {variant["$ref"].split("/")[-1] for variant in message["anyOf"]} == {
                "JSONRPCRequest",
                "JSONRPCNotification",
                "JSONRPCResponse",
                "JSONRPCError",
            }

        def initialize_initialized_is_the_only_client_notification() -> None:
            methods = schema_methods(wire("ClientNotification.json"))
            assert methods == ["initialized"]

        def turn_method_shapes() -> None:
            schema = wire("ClientRequest.json")
            defs = schema["definitions"]

            steer = _params_of(schema, "turn/steer")
            steer_def = defs[steer["$ref"].split("/")[-1]]
            assert steer_def["required"] == ["expectedTurnId", "input", "threadId"]

            interrupt = _params_of(schema, "turn/interrupt")
            interrupt_def = defs[interrupt["$ref"].split("/")[-1]]
            assert interrupt_def["required"] == ["threadId", "turnId"]

            start = _params_of(schema, "turn/start")
            start_def = defs[start["$ref"].split("/")[-1]]
            assert "threadId" in start_def["required"]
            assert "input" in start_def["required"]
            # turn/start carries no expectedTurnId: the server steers internally.
            assert "expectedTurnId" not in start_def["properties"]

            settings = _params_of(schema, "thread/settings/update")
            settings_def = defs[settings["$ref"].split("/")[-1]]
            assert "threadId" in settings_def["required"]
            assert {"model", "effort"} <= set(settings_def["properties"])

        def approval_decision_surface() -> None:
            schema = wire("ServerRequest.json")
            decision = schema["definitions"]["CommandExecutionApprovalDecision"]
            decisions = set()
            for variant in decision["oneOf"]:
                if "enum" in variant:
                    decisions.add(variant["enum"][0])
                else:
                    decisions.update(variant.get("properties", {}).keys())
            assert decisions == {
                "accept",
                "acceptForSession",
                "acceptWithExecpolicyAmendment",
                "applyNetworkPolicyAmendment",
                "decline",
                "cancel",
            }
            fixture = bundle("approval.json")
            assert set(fixture["approval_request"]["valid_decisions"]) == decisions

        # --- captured behaviour conformance ----------------------------------

        def handshake_facts() -> None:
            fixture = bundle("handshake.json")
            assert fixture["transport"].startswith("WebSocket")
            assert fixture["server_response_status_line"] == "HTTP/1.1 101 Switching Protocols"
            assert fixture["client_request_line"] == "GET / HTTP/1.1"
            assert fixture["initialize"]["required_notification"] == {"method": "initialized"}
            assert "jsonrpc" in fixture["jsonrpc_version_field"]

        def handshake_accept_derivation_known_answer() -> None:
            # RFC 6455 sample key/value pair.
            assert expected_accept("dGhlIHNhbXBsZSBub25jZQ==") == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="

        def thread_lifecycle_facts() -> None:
            fixture = bundle("thread_lifecycle.json")
            resume = fixture["thread_resume"]
            assert resume["subscribes_second_connection"] is True
            assert "turn/started" in resume["live_notification_methods_seen_by_second_client"]
            assert resume["error_before_rollout_exists"]["code"] == -32600
            assert (
                fixture["thread_start"]["thread_facts"]["rollout_exists_before_first_turn"] is False
            )
            assert (
                fixture["thread_start"]["thread_facts"]["rollout_exists_after_first_turn"] is True
            )
            fork = fixture["thread_fork"]
            assert fork["history_preserved"] is True
            assert "forked_from_id" in fork

        def turn_control_facts() -> None:
            fixture = bundle("turn_control.json")
            race = fixture["concurrent_submission_race"]
            assert race["response_turn_id_equals_active_turn_id"] is True
            steer = fixture["turn_steer"]
            assert steer["expectedTurnId_required"] is True
            assert steer["response"] == {"result": {"turnId": "<same active turn-id>"}}
            assert steer["stale_turn_refusal"]["error"]["code"] == -32600
            assert steer["stale_turn_refusal"]["error"]["message"] == "no active turn to steer"
            interrupt = fixture["turn_interrupt"]
            assert interrupt["completed_turn_status"] == "interrupted"
            assert interrupt["completed_turn_error"] is None

        def approval_facts() -> None:
            fixture = bundle("approval.json")
            assert (
                fixture["approval_request"]["method_fired"]
                == "item/commandExecution/requestApproval"
            )
            assert fixture["broadcast_behavior"]["delivered_to_every_subscribed_connection"] is True
            assert fixture["resolution"]["notification"] == "serverRequest/resolved"
            assert "never" in fixture["observer_rule"]

        def capability_gating_facts() -> None:
            fixture = bundle("capabilities.json")
            settings = fixture["thread_settings_update"]
            gate = settings["without_experimental_api"]
            # Older captures nest the error; newer ones do not.
            assert gate.get("error", gate) == {
                "code": -32600,
                "message": "thread/settings/update requires experimentalApi capability",
            }
            queue = fixture["native_queue"]
            assert queue["without_experimental_api"]["add_error"]["code"] == -32600
            assert "not use the native queue" in queue["theater_position"]

        def ui_topology_facts() -> None:
            fixture = bundle("ui_topology.json")
            assert fixture["topology"]["backend"].startswith("codex app-server --listen unix://")
            assert fixture["topology"]["ui"].startswith("codex --remote unix://")
            assert fixture["shared_session"]["observer_saw_ui_initiated"]
            assert "no_blind_sleep" in fixture["ui_readiness"]
            survival = fixture["survival"]["abrupt_control_client_death"]
            assert survival["backend_alive"] and survival["ui_alive"]
            assert survival["reconnect"].startswith("a fresh control client")
            remote = fixture["remote_flag"]
            assert "no embedded backend was started" in remote["bad_endpoint"]["no_silent_fallback"]

        def unsupported_capabilities_have_explicit_reasons() -> None:
            fixture = bundle("unsupported_capabilities.json")
            assert len(fixture["entries"]) >= 5
            for entry in fixture["entries"]:
                assert entry["status"] in {"unavailable", "not used by design", "not exercised"}
                assert entry["reason"], f"missing reason for {entry['capability']}"

        def zero_turn_error_consistent_across_fixtures() -> None:
            lifecycle = bundle("thread_lifecycle.json")
            topology = bundle("ui_topology.json")
            assert (
                "no rollout found"
                in lifecycle["thread_resume"]["native_ui_resume_before_first_turn_error"]
            )
            assert "no rollout found" in topology["remote_flag"]["bad_endpoint"]["error_text"]

        run_rows(
            [
                ("protocol:installed_release_facts", installed_release_facts),
                ("protocol:required_client_methods_present", required_client_methods_present),
                ("protocol:required_approval_methods_present", required_approval_methods_present),
                ("protocol:required_notifications_present", required_notifications_present),
                ("protocol:wire_messages_omit_jsonrpc_version", wire_messages_omit_jsonrpc_version),
                (
                    "protocol:initialize_initialized_is_the_only_client_notification",
                    initialize_initialized_is_the_only_client_notification,
                ),
                ("protocol:turn_method_shapes", turn_method_shapes),
                ("protocol:approval_decision_surface", approval_decision_surface),
                ("behaviour:handshake_facts", handshake_facts),
                (
                    "behaviour:handshake_accept_derivation_known_answer",
                    handshake_accept_derivation_known_answer,
                ),
                ("behaviour:thread_lifecycle_facts", thread_lifecycle_facts),
                ("behaviour:turn_control_facts", turn_control_facts),
                ("behaviour:approval_facts", approval_facts),
                ("behaviour:capability_gating_facts", capability_gating_facts),
                ("behaviour:ui_topology_facts", ui_topology_facts),
                (
                    "behaviour:unsupported_capabilities_have_explicit_reasons",
                    unsupported_capabilities_have_explicit_reasons,
                ),
                (
                    "behaviour:zero_turn_error_consistent_across_fixtures",
                    zero_turn_error_consistent_across_fixtures,
                ),
            ]
        )


class TestBundleIndex:
    """The retained evidence: exactly the floor and latest bundles, complete and indexed."""

    def test_retained_bundles_are_complete_and_indexed(self) -> None:
        bundles = load_index()["bundles"]
        assert set(bundles) == set(RETAINED_BUNDLES)
        for role, entry in bundles.items():
            assert entry["status"] == "qualified", f"{role} must be qualified"
            directory = bundle_dir(role)
            for name in BUNDLE_BEHAVIOR_FILES:
                assert (directory / name).is_file(), f"{role}/{name} is missing"
            for name in BUNDLE_SCHEMA_FILES:
                assert (directory / "protocol_schema" / name).is_file(), (
                    f"{role}/protocol_schema/{name} is missing"
                )

    def test_no_unindexed_bundle_directories(self) -> None:
        # candidates/ holds untracked captures and is never indexed as supported.
        indexed = {entry["directory"] for entry in load_index()["bundles"].values()}
        for child in FIXTURES.iterdir():
            if child.is_dir() and child.name != CANDIDATES_DIRNAME:
                assert child.name in indexed, f"{child} is a bundle directory not in index.json"
