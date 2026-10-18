"""OpenCode detached-server launch plan and stdout endpoint discovery."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests.support.opencode_probe import patch_probe
from tests.support.opencode_releases import V1_RELEASE, V2_RELEASE, V2_UNQUALIFIED
from theater import paths
from theater.harness.builtin.plugins.opencode.dialect import (
    domain_for,
    v2_database_path,
    v2_lineage_marker,
)
from theater.harness.builtin.plugins.opencode.manifest import MANIFEST
from theater.harness.builtin.plugins.opencode.mcp import plugin_path
from theater.harness.builtin.plugins.opencode.observer import database_path
from theater.harness.builtin.plugins.opencode.runtime_plan import (
    OPENCODE_SERVER_V2_COMPATIBILITY_POLICY,
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
    RuntimeProbeContext,
    RuntimeSessionOrder,
)


def _context(tmp_path: Path, **overrides: object) -> RuntimePlanningContext:
    fields: dict[str, object] = {
        "participant_id": "h00000000001",
        "cwd": str(tmp_path),
        "token_file": tmp_path / "runtime.token",
    }
    fields.update(overrides)
    return RuntimePlanningContext(**fields)  # type: ignore[arg-type]


@pytest.fixture
def fake_opencode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Plan-time binary resolution needs an executable; `--version` answers 1.x."""
    fake = tmp_path / "bin" / "opencode"
    fake.parent.mkdir()
    fake.write_text(
        '#!/bin/sh\nif [ "$1" = "--version" ]; then '
        f'printf "opencode {V1_RELEASE}\\n"; exit 0; fi\n'
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
        "OPENCODE_DB": str(database_path()),
    }
    assert plan.backend.secret_env == {SERVER_SECRET_ENV: context.token_file}
    assert plan.backend.receipt_token_path == (
        paths.participant_observation_dir("h00000000001", "opencode") / "receipt-token"
    )
    config = json.loads(plan.backend.files[config_path])
    assert config["model"] == "mistral/mistral-large-latest"
    assert config["plugin"] == [plugin_path(config_path).resolve().as_uri()]
    assert plugin_path(config_path) in plan.backend.files
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


def test_a_2x_release_serves_its_own_lineage_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_opencode
) -> None:
    monkeypatch.setenv("THEATER_OPENCODE_VERSION", V2_RELEASE)
    plan = plan_opencode_server(_context(tmp_path, approval="manual"))
    database = v2_database_path("h00000000001")
    assert plan.backend.argv == [
        str(fake_opencode.resolve()),
        "serve",
        "--hostname",
        "127.0.0.1",
        "--port",
        "0",
    ]
    assert plan.backend.env["OPENCODE_DB"] == str(database)
    assert plan.backend.transcript_domain == domain_for(database)
    assert v2_lineage_marker("h00000000001")[0] in plan.backend.files

    patch_probe(monkeypatch, f"opencode v{V2_RELEASE}\n", "--port --hostname")
    qualified = probe_opencode_server_compatibility(RuntimeProbeContext(binary="opencode"))
    assert (qualified.supported, qualified.native_version) == (True, V2_RELEASE)
    assert qualified.policy == OPENCODE_SERVER_V2_COMPATIBILITY_POLICY
    patch_probe(monkeypatch, f"opencode v{V2_UNQUALIFIED}\n", "--port --hostname")
    assert not probe_opencode_server_compatibility(RuntimeProbeContext(binary="opencode")).supported


@pytest.mark.parametrize(
    "line",
    [
        "opencode server listening on http://127.0.0.1:4096\n",
        "server listening on http://127.0.0.1:4096\n",
    ],
)
def test_banner_yields_loopback_origin(line: str) -> None:
    assert parse_server_stdout_endpoint(line) == "http://127.0.0.1:4096"


def test_non_banner_lines_are_silence() -> None:
    assert parse_server_stdout_endpoint("some other log line") is None
    assert parse_server_stdout_endpoint("server password abc") is None
    assert parse_server_stdout_endpoint("") is None


@pytest.mark.parametrize(
    "line",
    [
        "opencode server listening on ",
        "opencode server listening on http://localhost:1",
        "opencode server listening on http://0.0.0.0:1",
        "opencode server listening on https://127.0.0.1:1",
        "opencode server listening on http://127.0.0.1:1/p",
        "opencode server listening on http://127.0.0.1:notaport",
        "server listening on http://0.0.0.0:1",
    ],
)
def test_malformed_announcements_fail_closed(line: str) -> None:
    with pytest.raises(ValueError):
        parse_server_stdout_endpoint(line)
