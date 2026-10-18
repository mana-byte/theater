"""OpenCode 2.x launch planning: private server, lineage database, resume and approval."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys

import pytest

from tests.support.opencode_recorder_v2 import rec as shared_rec
from tests.support.shipped import OpenCodeHarness
from theater.harness import theater_mcp_servers
from theater.harness.builtin.plugins.opencode.dialect import (
    OpenCodeDialect,
    domain_for,
    v2_database_path,
)
from theater.harness.builtin.plugins.opencode.native_plugin_v2 import (
    plugin_dir,
)
from theater.harness.builtin.plugins.opencode.runtime_plan import parse_opencode_version
from theater.harness.contracts.context import ParticipantObservationContext
from theater.models import BadRequest, Participant

rec = shared_rec  # the shared fixture, under the name the tests ask for


V2 = OpenCodeDialect.V2


@pytest.fixture
def fake_opencode(tmp_path, monkeypatch):
    """Plan-time binary resolution needs an executable; this one logs every invocation."""
    fake = tmp_path / "bin" / "opencode"
    fake.parent.mkdir()
    fake.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> {tmp_path / "calls"}\n')
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake.parent}:{os.environ['PATH']}")
    return fake


def _bootstrap_settings(plan, config) -> dict:
    """The settings embedded in the generated launcher, decoded as the launcher does."""
    script = plan.files[config.with_suffix(".bootstrap.py")]
    literal = script.rsplit("main(json.loads(", 1)[1].removesuffix(")))\n")
    return json.loads(json.loads(literal))


def test_the_2x_version_banner_names_its_release():
    assert parse_opencode_version("opencode v2.0.18\n") == (2, 0, 18)
    assert parse_opencode_version("1.18.29") == (1, 18, 29)


def test_a_2x_launch_runs_a_private_server_on_its_own_database(
    tmp_path, monkeypatch, fake_opencode
):
    monkeypatch.setenv("THEATER_OPENCODE_VERSION", "2.0.18")
    config = tmp_path / "abc.json"
    plan = OpenCodeHarness(dialect=V2).plan_launch(
        participant_id="abc123",
        prompt="say hello",
        config_path=config,
        approval="manual",
        model="anthropic/claude",
        mcp_servers=theater_mcp_servers("abc123", "opencode"),
    )

    database = v2_database_path("abc123")
    bootstrap = config.with_suffix(".bootstrap.py")
    assert plan.argv == [sys.executable, str(bootstrap)]
    assert plan.env == {"OPENCODE_CONFIG": str(config), "OPENCODE_DB": str(database)}
    assert plan.session_id is not None and plan.session_id.startswith("ses_")
    assert plan.transcript_domain == domain_for(database)
    assert database.parent / ".opencode-v2" in plan.files
    document = json.loads(plan.files[config])
    assert document["plugin"] == [plugin_dir(config).resolve().as_uri()]
    assert document["model"] == "anthropic/claude"
    theater = document["mcp"]["servers"]["theater"]
    assert theater["type"] == "local" and theater["codemode"] is False
    assert "enabled" not in theater
    package = json.loads(plan.files[plugin_dir(config) / "package.json"])
    assert package["type"] == "module"
    assert "export default" in plan.files[plugin_dir(config) / "server.js"]
    settings = _bootstrap_settings(plan, config)
    assert settings["binary"] == str(fake_opencode.resolve())
    assert settings["approval"] == "manual"
    assert settings["session_id"] == plan.session_id
    assert settings["prompt"] == "say hello"
    assert settings["plugin_source"] == str((plugin_dir(config) / "server.js").resolve())
    # A `cli.json` autoaccept would otherwise answer the plugin's asks in the client.
    assert settings["tui_env"] == {
        "OPENCODE_CLI_CONFIG_CONTENT": '{"session": {"permissions": "prompt"}}'
    }
    assert "credential" in settings and "server-credential" in settings["credential"]


def test_a_promptless_enforced_launch_still_precreates_its_protected_session(
    tmp_path, monkeypatch, fake_opencode
):
    monkeypatch.setenv("THEATER_OPENCODE_VERSION", "2.0.18")
    config = tmp_path / "abc.json"
    plan = OpenCodeHarness(dialect=V2).plan_launch(
        participant_id="abc123",
        prompt="",
        config_path=config,
        approval="edits",
    )
    assert plan.session_id is not None and plan.session_id.startswith("ses_")
    settings = _bootstrap_settings(plan, config)
    assert settings["prompt"] is None and settings["resume"] is None
    assert settings["approval"] == "edits"


def test_an_enforced_resume_verifies_its_plugin_before_any_ui(tmp_path, monkeypatch, fake_opencode):
    monkeypatch.setenv("THEATER_OPENCODE_VERSION", "2.0.18")
    config = tmp_path / "abc.json"
    plan = OpenCodeHarness(dialect=V2).plan_launch(
        participant_id="abc123",
        prompt="say hello",
        config_path=config,
        approval="manual",
        resume="ses_resume",
    )
    assert plan.session_id == "ses_resume"
    settings = _bootstrap_settings(plan, config)
    assert settings["resume"] == "ses_resume"
    assert settings["prompt"] is None and settings["session_id"] is None


def test_an_unqualified_2x_release_refuses_enforced_approval(tmp_path, monkeypatch, fake_opencode):
    monkeypatch.setenv("THEATER_OPENCODE_VERSION", "2.1.0")
    with pytest.raises(BadRequest, match=r"qualified 2\.x range"):
        OpenCodeHarness(dialect=V2).plan_launch(
            participant_id="abc123",
            prompt="say hello",
            config_path=tmp_path / "abc.json",
            approval="manual",
        )
    # Yolo enforces nothing, so it keeps its ordinary launch on any 2.x release.
    plan = OpenCodeHarness(dialect=V2).plan_launch(
        participant_id="abc123",
        prompt="say hello",
        config_path=tmp_path / "abc.json",
        approval="yolo",
    )
    assert "--auto" in plan.argv


def test_a_new_session_exists_before_its_tui_opens_on_it(tmp_path, fake_opencode):
    """2.0.18 drops a cold home screen's `--prompt`; a session's screen submits it once ready."""
    plan = OpenCodeHarness(dialect=V2).plan_launch(
        participant_id="abc123",
        prompt="say hello",
        config_path=tmp_path / "abc.json",
        approval="yolo",
    )
    subprocess.run(plan.argv, env=os.environ, check=True, timeout=30)

    created, opened = (tmp_path / "calls").read_text().splitlines()
    session = json.loads(created.removeprefix("api --standalone POST /api/session -d "))["id"]
    assert session.startswith("ses_")
    assert opened == f"--standalone --auto --prompt say hello -s {session}"


def test_a_failed_session_create_never_opens_the_tui(tmp_path, fake_opencode):
    """The prompt must not be lost to a TUI that opens on a session that does not exist."""
    fake_opencode.write_text(
        f'#!/bin/sh\nprintf "%s\\n" "$*" >> {tmp_path / "calls"}\ncase "$1" in api) exit 1;; esac\n'
    )
    plan = OpenCodeHarness(dialect=V2).plan_launch(
        participant_id="abc123",
        prompt="say hello",
        config_path=tmp_path / "abc.json",
        approval="yolo",
    )
    run = subprocess.run(plan.argv, env=os.environ, capture_output=True, timeout=30, check=False)
    assert run.returncode != 0
    (only,) = (tmp_path / "calls").read_text().splitlines()
    assert only.startswith("api --standalone POST /api/session -d ")


def test_yolo_auto_accepts_and_a_resume_continues_its_known_session(tmp_path, fake_opencode):
    config = tmp_path / "abc.json"
    plan = OpenCodeHarness(dialect=V2).plan_launch(
        participant_id="abc123",
        prompt="",
        config_path=config,
        approval="yolo",
        resume="ses_1",
    )
    assert plan.argv == [str(fake_opencode.resolve()), "--standalone", "--auto", "-s", "ses_1"]
    assert "OPENCODE_CLI_CONFIG_CONTENT" not in plan.env
    assert plan.session_id == "ses_1"
    assert '"rules": []' in plan.files[plugin_dir(config) / "server.js"]


def test_a_resume_stays_in_its_lineage_and_never_crosses_releases(tmp_path):
    lineage = v2_database_path("first")
    lineage.parent.mkdir(parents=True)
    lineage.touch()
    predecessor = Participant(id="second", transcript_domain=domain_for(lineage))

    overlay = OpenCodeHarness(dialect=V2).resume_launch_overlay(
        predecessor=predecessor, trusted_session_owners=(predecessor,)
    )
    assert overlay.env == {"OPENCODE_DB": str(lineage.resolve())}
    assert overlay.transcript_domain == domain_for(lineage)

    with pytest.raises(BadRequest, match="not a Theater-isolated"):
        OpenCodeHarness(dialect=V2).resume_launch_overlay(
            predecessor=predecessor, trusted_session_owners=()
        )
    v1_domain = f"opencode://{(tmp_path / 'opencode.db').resolve()}"
    with pytest.raises(BadRequest, match=r"recorded by OpenCode 1\.x"):
        OpenCodeHarness(dialect=V2).resume_launch_overlay(
            predecessor=Participant(id="old", transcript_domain=v1_domain),
            trusted_session_owners=(),
        )
    with pytest.raises(BadRequest, match=r"recorded by OpenCode 2\.x"):
        OpenCodeHarness(db=tmp_path / "opencode.db").resume_launch_overlay(
            predecessor=predecessor, trusted_session_owners=(predecessor,)
        )


def test_a_2x_domain_opens_its_own_lineage_database(rec, workdir):
    context = ParticipantObservationContext(
        participant_id="abc123", cwd=str(workdir), transcript_domain=domain_for(rec.path)
    )
    src = OpenCodeHarness().observer.open_source_context(context)
    assert src.collision_domain == domain_for(rec.path)
    batch = asyncio.run(src.read())
    assert batch.attached is not None and batch.attached.session_id == "ses_one"
