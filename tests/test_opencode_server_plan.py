"""Planning and qualification for the detached OpenCode server topology."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from theater import paths
from theater.harness.builtin.plugins.opencode import server_plan
from theater.harness.builtin.plugins.opencode.mcp import plugin_path
from theater.harness.builtin.plugins.opencode.observer import database_path
from theater.harness.builtin.plugins.opencode.server_discovery import parse_server_stdout_endpoint
from theater.harness.builtin.plugins.opencode.server_plan import (
    SERVER_SECRET_ENV,
    plan_opencode_server,
    probe_opencode_server_compatibility,
)
from theater.harness.contracts.runtime import RuntimePlanningContext, RuntimeProbeContext


def _context(tmp_path: Path, **overrides: object) -> RuntimePlanningContext:
    fields: dict[str, object] = {
        "participant_id": "h00000000001",
        "cwd": str(tmp_path),
        "token_file": tmp_path / "runtime.token",
    }
    fields.update(overrides)
    return RuntimePlanningContext(**fields)  # type: ignore[arg-type]


def test_plan_requires_the_core_minted_credential(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="credential"):
        plan_opencode_server(_context(tmp_path, token_file=None))


def test_plan_builds_the_stock_serve_backend(tmp_path: Path) -> None:
    context = _context(tmp_path, model="mistral/mistral-large-latest", approval="edits")
    plan = plan_opencode_server(context)

    config_path = paths.mcp_config_path("h00000000001")
    assert plan.backend.argv == ["opencode", "serve", "--hostname", "127.0.0.1", "--port", "0"]
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


def test_plan_omits_an_unset_model(tmp_path: Path) -> None:
    plan = plan_opencode_server(_context(tmp_path))
    config = json.loads(plan.backend.files[paths.mcp_config_path("h00000000001")])
    assert "model" not in config


class _Result:
    def __init__(self, output: str) -> None:
        self.returncode = 0
        self.stdout = output
        self.stderr = ""


def _patch_probe(monkeypatch: pytest.MonkeyPatch, version: str, serve_help: str) -> None:
    def run(argv: list[str], **kwargs: object) -> _Result:
        del kwargs
        return _Result(version) if argv[-1] == "--version" else _Result(serve_help)

    monkeypatch.setattr(server_plan.subprocess, "run", run)


def test_probe_qualifies_the_probed_release(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_probe(monkeypatch, "opencode 1.18.29+c470c79\n", "--port --hostname")
    compatibility = probe_opencode_server_compatibility(RuntimeProbeContext(binary="opencode"))
    assert compatibility.supported is True
    assert compatibility.native_version == "1.18.29"


def test_probe_rejects_releases_outside_the_window(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_probe(monkeypatch, "1.18.28", "--port --hostname")
    compatibility = probe_opencode_server_compatibility(RuntimeProbeContext(binary="opencode"))
    assert compatibility.supported is False
    assert "compatibility range" in (compatibility.reason or "")


def test_probe_requires_the_serve_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_probe(monkeypatch, "1.18.29", "--model --auto")
    compatibility = probe_opencode_server_compatibility(RuntimeProbeContext(binary="opencode"))
    assert compatibility.supported is False
    assert "port-0 flags" in (compatibility.reason or "")


def test_probe_reports_probe_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    def run(argv: list[str], **kwargs: object) -> _Result:
        del argv, kwargs
        raise OSError("no binary")

    monkeypatch.setattr(server_plan.subprocess, "run", run)
    compatibility = probe_opencode_server_compatibility(RuntimeProbeContext(binary="opencode"))
    assert compatibility.supported is False
    assert "probes" in (compatibility.reason or "")
