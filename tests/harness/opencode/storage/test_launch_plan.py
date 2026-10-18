"""OpenCode 1.x launch plans: id transport, approval enforcement and the injected database."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from tests.support.opencode_recorder import rec as shared_rec
from tests.support.shipped import OpenCodeHarness
from theater.harness import theater_mcp_servers
from theater.harness.builtin.plugins.opencode.mcp import plugin_path
from theater.harness.manifests.compiler import ManifestHarnessObserver
from theater.models import BadRequest, Participant

rec = shared_rec  # the shared fixture, under the name the tests ask for


@pytest.fixture
def opencode_binary(tmp_path, monkeypatch):
    binary = tmp_path / "bin" / "opencode"
    binary.parent.mkdir()
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", f"{binary.parent}:{os.environ['PATH']}")
    return str(binary.resolve())


def test_the_id_travels_in_a_merged_config_file(tmp_path, opencode_binary):
    config = tmp_path / "abc.json"
    database = tmp_path / "opencode.db"
    plan = OpenCodeHarness(db=database).plan_launch(
        participant_id="abc123",
        prompt="say hello",
        config_path=config,
        approval="yolo",
        mcp_servers=theater_mcp_servers("abc123", "opencode"),
    )

    assert plan.argv == [opencode_binary, "--auto", "--prompt", "say hello"]
    assert plan.env == {
        "OPENCODE_CONFIG": str(config),
        "OPENCODE_DB": str(database.resolve()),
    }
    document = json.loads(plan.files[config])
    server = document["mcp"]["theater"]
    assert server["command"][1:] == [
        "mcp",
        "--id",
        "abc123",
        "--harness",
        "opencode",
        "--toolset",
        "control",
    ]
    assert server["enabled"] is True
    plugin = tmp_path / "abc.opencode.mjs"
    assert document["plugin"] == [plugin.resolve().as_uri()]
    assert plugin in plan.files
    assert "session.created" in plan.files[plugin]
    assert "!info.parentID" in plan.files[plugin]
    assert "abc123" in plan.files[plugin]
    assert plan.receipt_token_path is not None
    assert str(plan.receipt_token_path) in plan.files[plugin]
    assert ".opencode-session" not in plan.files[plugin]


def test_every_approval_enforces_its_own_native_policy(tmp_path, opencode_binary):
    """OpenCode's permission layers merge in order: permissive agent
    defaults, then every config file (with OPENCODE_PERMISSION inside that
    same layer), then the selected agent's own config-file rules. On the TUI
    route no Theater-side proof can come between a broken plugin and the
    first model call, so manual/edits are refused here and enforced only on
    the server topology, where the session is refused unless the plugin
    proves it loaded; `--auto` is yolo's own auto-approve channel.
    """

    def plan(approval):
        return OpenCodeHarness().plan_launch(
            participant_id="abc123",
            prompt="",
            config_path=tmp_path / "x.json",
            approval=approval,
        )

    yolo = plan("yolo")
    assert yolo.argv == [opencode_binary, "--auto"]
    # `--auto` approves everything; an enforced ruleset would only fight it.
    source = yolo.files[plugin_path(tmp_path / "x.json")]
    start = source.index("const permissionRules = ")
    end = source.index("\n", start)
    assert json.loads(source[start + len("const permissionRules = ") : end]) == []

    for approval in ("manual", "edits"):
        with pytest.raises(BadRequest, match="cannot prove its approval plugin loaded"):
            plan(approval)


def test_approval_enforcement_refuses_a_resume_launch_too(tmp_path, opencode_binary):
    """A resume is the same TUI process shape, so a manual/edits resume has the
    same fail-open hole through a broken plugin — it is refused like a fresh
    launch, while a yolo resume still plans."""
    with pytest.raises(BadRequest, match="cannot prove its approval plugin loaded"):
        OpenCodeHarness().plan_launch(
            participant_id="abc123",
            prompt="",
            config_path=tmp_path / "x.json",
            approval="edits",
            resume="ses_1",
        )
    plan = OpenCodeHarness().plan_launch(
        participant_id="abc123",
        prompt="",
        config_path=tmp_path / "x.json",
        approval="yolo",
        resume="ses_1",
    )
    assert plan.argv == [opencode_binary, "--auto", "-s", "ses_1", "--fork"]


def test_an_unknown_approval_is_refused(tmp_path):
    with pytest.raises(BadRequest):
        OpenCodeHarness().plan_launch(
            participant_id="abc123",
            prompt="",
            config_path=tmp_path / "x.json",
            approval="whatever",
        )


def test_configured_manifest_reads_the_injected_database_and_correlation(
    rec, workdir, tmp_path, opencode_binary
):
    correlation = tmp_path / "correlation"
    correlation.mkdir()
    (correlation / "participant.opencode.mjs").write_text("")
    harness = OpenCodeHarness(db=rec.path, correlation_dir=correlation)
    assert isinstance(harness.observer, ManifestHarnessObserver)
    launch = harness.plan_launch(
        participant_id="participant",
        prompt="",
        config_path=tmp_path / "config.json",
        approval="yolo",
    )
    assert launch.env["OPENCODE_DB"] == str(rec.path.resolve())
    candidates = harness.observer.transcript_candidates(cwd=str(workdir))
    assert [candidate.domain for candidate in candidates] == [f"opencode://{rec.path.resolve()}"]
    admitted = harness.observer.admit_operator_candidate(
        cwd=str(workdir), candidate="opencode://ses_one"
    )
    assert admitted.domain == f"opencode://{rec.path.resolve()}"

    source = harness.observer.open_source_for(participant_id="participant", cwd=str(workdir))
    assert source._db == rec.path
    assert source._receipt_expected is True
    assert (
        source.admit_exact_location(location="opencode://ses_one", session_id="ses_one") == "staged"
    )
    attached = asyncio.run(source.read())
    assert attached.attached is not None
    assert attached.attached.location == "opencode://ses_one"
    receipt = harness.observer.validate_transcript_receipt(
        payload={"session_id": "ses_one"}, cwd=str(workdir), expected_session_id=None
    )
    assert receipt.domain == f"opencode://{rec.path.resolve()}"


def test_configured_manifest_resume_overlay_uses_the_injected_database(tmp_path):
    database = tmp_path / "opencode.db"
    expected = f"opencode://{database.resolve()}"
    harness = OpenCodeHarness(db=database)
    predecessor = Participant(id="predecessor", transcript_domain=expected)

    assert (
        harness.resume_launch_overlay(
            predecessor=predecessor,
            trusted_session_owners=(),
        ).transcript_domain
        == expected
    )
    with pytest.raises(BadRequest, match="does not match the OpenCode observation"):
        harness.resume_launch_overlay(
            predecessor=Participant(id="wrong", transcript_domain="opencode://wrong.db"),
            trusted_session_owners=(),
        )


def test_resume_forks_the_session_and_waits_for_its_receipt(tmp_path, available_harness_binaries):
    """Manual/edits cannot be proven on the TUI route, so the resume keeps
    working for yolo — the enforced choices are covered by the refusal test."""
    with pytest.raises(BadRequest, match="cannot prove its approval plugin loaded"):
        OpenCodeHarness().plan_launch(
            participant_id="abc123",
            prompt="do something",
            config_path=tmp_path / "x.json",
            approval="manual",
            resume="ses_ffb42302cffeaasiFBDGgLmkRf",
        )
    plan = OpenCodeHarness().plan_launch(
        participant_id="abc123",
        prompt="do something",
        config_path=tmp_path / "x.json",
        approval="yolo",
        resume="ses_ffb42302cffeaasiFBDGgLmkRf",
    )
    assert plan.argv == [
        str(Path(sys.executable).resolve()),
        "--auto",
        "-s",
        "ses_ffb42302cffeaasiFBDGgLmkRf",
        "--fork",
    ]
    receipt = tmp_path / "x.opencode-session"
    assert receipt not in plan.files
    assert plan.session_id is None


def test_resume_with_model_and_auto(tmp_path, available_harness_binaries):
    """Model and approval flags still apply when resuming."""
    plan = OpenCodeHarness().plan_launch(
        participant_id="abc123",
        prompt="",
        config_path=tmp_path / "x.json",
        approval="yolo",
        model="openai-foundry/zai-glm-5-2",
        resume="ses_abc",
    )
    assert plan.argv == [
        str(Path(sys.executable).resolve()),
        "--model",
        "openai-foundry/zai-glm-5-2",
        "--auto",
        "-s",
        "ses_abc",
        "--fork",
    ]


def test_resume_takes_prompt_is_false():
    """opencode drops the prompt when resuming: `-s` routes to the session
    view and `--prompt` is only read on the home screen."""
    assert OpenCodeHarness.resume_takes_prompt is False
