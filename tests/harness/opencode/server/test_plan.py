"""OpenCode detached-server launch plan and stdout endpoint discovery."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from theater import paths
from theater.harness.builtin.plugins.opencode.dialect import (
    domain_for,
    lineage_marker,
    participant_database_path,
)
from theater.harness.builtin.plugins.opencode.manifest import MANIFEST
from theater.harness.builtin.plugins.opencode.native_plugin import plugin_dir
from theater.harness.builtin.plugins.opencode.observer import database_path
from theater.harness.builtin.plugins.opencode.runtime_plan import (
    VERSION_POLICY,
)
from theater.harness.builtin.plugins.opencode.server_discovery import (
    parse_server_stdout_endpoint,
)
from theater.harness.builtin.plugins.opencode.server_plan import (
    SERVER_SECRET_ENV,
    plan_opencode_server,
    probe_opencode_server_compatibility,
)
from theater.harness.contracts.runtime import (
    RuntimeCapability,
    RuntimeHost,
    RuntimePlanningContext,
    RuntimeSessionOrder,
)


def test_observed_default_database_is_distinct_from_participant_lineages(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))

    assert database_path() == tmp_path / "opencode" / "opencode.db"
    assert participant_database_path("test").name == "opencode-v2.db"
    assert database_path() != participant_database_path("test")


def _context(tmp_path: Path, **overrides: object) -> RuntimePlanningContext:
    fields: dict[str, object] = {
        "participant_id": "h00000000001",
        "cwd": str(tmp_path),
        "token_file": tmp_path / "runtime.token",
        "approval": "manual",
    }
    fields.update(overrides)
    return RuntimePlanningContext(**fields)  # type: ignore[arg-type]


@pytest.fixture
def fake_opencode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Resolve a disposable executable matching the installed support floor."""
    fake = tmp_path / "bin" / "opencode"
    fake.parent.mkdir()
    fake.write_text(
        '#!/bin/sh\nif [ "$1" = "--version" ]; then '
        f'printf "opencode {VERSION_POLICY.minimum}\\n"; exit 0; fi\n'
        'printf "unexpected invocation: %s\\n" "$*" >&2\nexit 1\n'
    )
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake.parent}:{os.environ['PATH']}")
    return fake


def test_plan_requires_the_core_minted_credential(tmp_path: Path, fake_opencode) -> None:
    with pytest.raises(ValueError, match="credential"):
        plan_opencode_server(_context(tmp_path, token_file=None))


def test_plan_builds_the_stock_serve_backend(tmp_path: Path, fake_opencode) -> None:
    context = _context(tmp_path, model="mistral/mistral-large-latest", approval="edits")
    plan = plan_opencode_server(context)

    config_path = paths.mcp_config_path("h00000000001")
    assert plan.backend.argv == [
        str(fake_opencode.resolve()),
        "serve",
        "--hostname",
        "127.0.0.1",
        "--port",
        "0",
    ]
    assert plan.backend.env == {
        "OPENCODE_CONFIG": str(config_path),
        "OPENCODE_DB": str(participant_database_path("h00000000001")),
    }
    assert plan.backend.secret_env == {SERVER_SECRET_ENV: context.token_file}
    assert plan.backend.receipt_token_path == (
        paths.participant_observation_dir("h00000000001", "opencode") / "receipt-token"
    )
    config = json.loads(plan.backend.files[config_path])
    assert config["model"] == "mistral/mistral-large-latest"
    assert config["plugin"] == [plugin_dir(config_path).resolve().as_uri()]
    assert plugin_dir(config_path) / "server.js" in plan.backend.files
    assert lineage_marker("h00000000001")[0] in plan.backend.files
    assert plan.backend.transcript_domain == domain_for(participant_database_path("h00000000001"))
    rendered = json.dumps({str(key): value for key, value in plan.backend.files.items()})
    assert "password" not in rendered.lower()
    assert plan.endpoint is None
    assert plan.endpoint_discovery is not None
    assert plan.endpoint_discovery.parser is parse_server_stdout_endpoint


def test_plan_omits_an_unset_model(tmp_path: Path, fake_opencode) -> None:
    plan = plan_opencode_server(_context(tmp_path))
    config = json.loads(plan.backend.files[paths.mcp_config_path("h00000000001")])
    assert "model" not in config


def test_manifest_selects_the_detached_server_runtime() -> None:
    runtime = MANIFEST.runtime
    assert runtime is not None
    assert runtime.host is RuntimeHost.DETACHED_BACKEND
    assert runtime.plan is plan_opencode_server
    assert runtime.probe is probe_opencode_server_compatibility
    assert runtime.session_order is RuntimeSessionOrder.SESSION_FIRST
    assert runtime.endpoint_discovery is not None
    assert runtime.endpoint_discovery.parser is parse_server_stdout_endpoint
    assert runtime.runtime_credential is not None
    assert runtime.runtime_credential.env == (SERVER_SECRET_ENV,)
    assert runtime.legacy_fallback == frozenset({RuntimeCapability.INTERRUPT})
    assert runtime.unavailable_capabilities == {
        RuntimeCapability.STEER,
        RuntimeCapability.SETTINGS_UPDATE,
    }
    assert runtime.channel.drives_job_completion is False


def test_banner_yields_loopback_origin() -> None:
    assert (
        parse_server_stdout_endpoint("server listening on http://127.0.0.1:4096\n")
        == "http://127.0.0.1:4096"
    )


def test_non_banner_lines_are_silence() -> None:
    assert parse_server_stdout_endpoint("some other log line") is None
    assert parse_server_stdout_endpoint("server password abc") is None
    assert parse_server_stdout_endpoint("") is None


@pytest.mark.parametrize(
    "line",
    [
        "server listening on ",
        "server listening on http://localhost:1",
        "server listening on http://0.0.0.0:1",
        "server listening on https://127.0.0.1:1",
        "server listening on http://127.0.0.1:1/p",
        "server listening on http://127.0.0.1:notaport",
    ],
)
def test_malformed_announcements_fail_closed(line: str) -> None:
    with pytest.raises(ValueError):
        parse_server_stdout_endpoint(line)
