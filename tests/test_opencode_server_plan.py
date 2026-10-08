"""Planning and qualification for the detached OpenCode server topology."""

from __future__ import annotations

import json
import os
import subprocess
import threading
from pathlib import Path

import pytest

from theater import paths
from theater.harness.builtin.plugins.opencode import server_plan
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
from theater.harness.builtin.plugins.opencode.server_discovery import parse_server_stdout_endpoint
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
        '#!/bin/sh\nif [ "$1" = "--version" ]; then printf "opencode 1.18.29\\n"; exit 0; fi\n'
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


class _Result:
    def __init__(self, output: str, returncode: int = 0) -> None:
        self.returncode = returncode
        self.stdout = output
        self.stderr = ""


def _patch_probe(
    monkeypatch: pytest.MonkeyPatch,
    version: str,
    serve_help: str,
    returncodes: tuple[int, int] = (0, 0),
) -> None:
    started = threading.Barrier(2, timeout=5)

    def run(argv: list[str], **kwargs: object) -> _Result:
        assert kwargs == {
            "capture_output": True,
            "text": True,
            "timeout": server_plan.MODELS_TIMEOUT,
            "check": False,
        }
        started.wait()
        return (
            _Result(version, returncodes[0])
            if argv[-1] == "--version"
            else _Result(serve_help, returncodes[1])
        )

    monkeypatch.setattr(server_plan.subprocess, "run", run)


@pytest.mark.parametrize("returncodes", [(0, 0), (1, 0), (0, 1)])
def test_probe_requires_both_successful_checks(
    monkeypatch: pytest.MonkeyPatch, returncodes: tuple[int, int]
) -> None:
    _patch_probe(monkeypatch, "opencode 1.18.29+c470c79\n", "--port --hostname", returncodes)
    compatibility = probe_opencode_server_compatibility(RuntimeProbeContext(binary="opencode"))
    assert compatibility.supported is (returncodes == (0, 0))
    if compatibility.supported:
        assert compatibility.native_version == "1.18.29"


@pytest.mark.parametrize(
    ("version", "supported"),
    [("1.17.14", False), ("1.17.15", True), ("1.18.34", True), ("1.18.35", False)],
)
def test_probe_qualifies_exactly_the_1x_window(
    monkeypatch: pytest.MonkeyPatch, version: str, supported: bool
) -> None:
    _patch_probe(monkeypatch, version, "--port --hostname")
    compatibility = probe_opencode_server_compatibility(RuntimeProbeContext(binary="opencode"))
    assert compatibility.supported is supported
    if not supported:
        assert "compatibility range" in (compatibility.reason or "")


def test_probe_requires_the_serve_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_probe(monkeypatch, "1.18.29", "--model --auto")
    compatibility = probe_opencode_server_compatibility(RuntimeProbeContext(binary="opencode"))
    assert compatibility.supported is False
    assert "port-0 flags" in (compatibility.reason or "")


@pytest.mark.parametrize(
    ("version", "supported"),
    [("2.0.5", False), ("2.0.6", True), ("2.0.20", True), ("2.1.0", False)],
)
def test_probe_qualifies_exactly_the_2x_window(
    monkeypatch: pytest.MonkeyPatch, version: str, supported: bool
) -> None:
    _patch_probe(monkeypatch, f"opencode v{version}\n", "--port --hostname")
    compatibility = probe_opencode_server_compatibility(RuntimeProbeContext(binary="opencode"))
    assert compatibility.supported is supported
    if not supported:
        assert "compatibility range" in (compatibility.reason or "")


def test_a_2x_release_serves_its_own_lineage_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_opencode
) -> None:
    monkeypatch.setenv("THEATER_OPENCODE_VERSION", "2.0.18")
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

    _patch_probe(monkeypatch, "opencode v2.0.18\n", "--port --hostname")
    qualified = probe_opencode_server_compatibility(RuntimeProbeContext(binary="opencode"))
    assert (qualified.supported, qualified.native_version) == (True, "2.0.18")
    assert qualified.policy == OPENCODE_SERVER_V2_COMPATIBILITY_POLICY
    _patch_probe(monkeypatch, "opencode v2.1.0\n", "--port --hostname")
    assert not probe_opencode_server_compatibility(RuntimeProbeContext(binary="opencode")).supported


def test_a_2x_resume_probe_refuses_before_any_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """A resume continues in the lineage database, which the native backend cannot open."""
    _patch_probe(monkeypatch, "opencode v2.0.18\n", "--port --hostname")
    refused = probe_opencode_server_compatibility(
        RuntimeProbeContext(binary="opencode", resume=True)
    )
    assert refused.supported is False
    assert "legacy route" in (refused.reason or "")


def test_a_1x_resume_probe_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_probe(monkeypatch, "opencode 1.18.29+c470c79\n", "--port --hostname")
    qualified = probe_opencode_server_compatibility(
        RuntimeProbeContext(binary="opencode", resume=True)
    )
    assert qualified.supported is True
    assert qualified.native_version == "1.18.29"


@pytest.mark.parametrize("failing_check", ["--version", "--help"])
@pytest.mark.parametrize("failure", [OSError("no binary"), subprocess.TimeoutExpired("probe", 1)])
def test_probe_reports_probe_failures_and_joins_both_checks(
    monkeypatch: pytest.MonkeyPatch, failing_check: str, failure: Exception
) -> None:
    started = threading.Barrier(2, timeout=5)
    finished: set[str] = set()

    def run(argv: list[str], **kwargs: object) -> _Result:
        del kwargs
        started.wait()
        try:
            if argv[-1] == failing_check:
                raise failure
            return _Result("1.18.29" if argv[-1] == "--version" else "--port --hostname")
        finally:
            finished.add(argv[-1])

    monkeypatch.setattr(server_plan.subprocess, "run", run)
    compatibility = probe_opencode_server_compatibility(RuntimeProbeContext(binary="opencode"))
    assert compatibility.supported is False
    assert "probes" in (compatibility.reason or "")
    assert finished == {"--version", "--help"}
