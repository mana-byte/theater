"""`theater skills`: daemon wire, rendering and degraded daemons."""

from __future__ import annotations

import json

import pytest

from tests.support.cli_fakes import daemon_stub, parse
from theater import cli
from theater.cli.commands import skills as skills_mod
from theater.protocol import RemoteError


def test_skills_main_skips_harness_installation(monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("skills must not install harness plugins")

    seen = []

    def command(args):
        seen.append(args.json)
        return 0

    monkeypatch.setattr(cli.harness_registry, "install", explode)
    monkeypatch.setitem(cli._COMMANDS, "skills", command)

    assert cli.main(["skills", "--json"]) == 0
    assert seen == [True]


def test_skills_uses_the_running_daemon_and_renders_diagnostics(monkeypatch, capsys):
    reply = {
        "skills": [{"name": "alpha", "description": "An alpha skill.", "source": "user"}],
        "rejections": [
            {"name": "broken", "path": "/tmp/skills/broken", "error": "invalid frontmatter"}
        ],
    }
    stub = daemon_stub(
        reply=reply, expect_kwargs={"autostart": False}, expect_call=("skills.list", {})
    )
    monkeypatch.setattr(skills_mod, "DaemonClient", stub)
    assert cli.cmd_skills(parse("skills")) == 0
    out = capsys.readouterr().out
    assert "alpha" in out and "An alpha skill." in out
    assert "rejected skill packages" in out
    assert "/tmp/skills/broken" in out and "invalid frontmatter" in out


def test_skills_json_is_the_daemon_wire_object(monkeypatch, capsys):
    data = {"skills": [{"name": "alpha", "description": "A.", "source": "user"}], "rejections": []}

    stub = daemon_stub(reply=data, expect_kwargs={"autostart": False})
    monkeypatch.setattr(skills_mod, "DaemonClient", stub)
    assert cli.cmd_skills(parse("skills", "--json")) == 0
    assert json.loads(capsys.readouterr().out) == data


def test_skills_human_output_removes_terminal_controls(monkeypatch, capsys):
    monkeypatch.setattr(
        skills_mod,
        "_skills_snapshot",
        lambda: {
            "skills": [
                {
                    "name": "line\nbreak",
                    "source": "u\tser",
                    "description": "escape\x1b[31m",
                }
            ],
            "rejections": [
                {
                    "name": "broken\rname",
                    "path": "/tmp/\x1bpath",
                    "error": "bad\nerror",
                }
            ],
        },
    )

    assert cli.cmd_skills(parse("skills")) == 0
    output = capsys.readouterr().out

    assert "\x1b" not in output
    assert "\t" not in output
    assert "\r" not in output
    assert len(output.splitlines()) == 5


def test_skills_reports_an_unavailable_daemon_without_starting_one(monkeypatch, capsys):
    stub = daemon_stub(enter_raises=FileNotFoundError(), expect_kwargs={"autostart": False})
    monkeypatch.setattr(skills_mod, "DaemonClient", stub)
    assert cli.main(["skills"]) == 1
    assert "no daemon running" in capsys.readouterr().err


def test_skills_reports_a_daemon_that_predates_the_rpc(monkeypatch, capsys):
    stub = daemon_stub(
        raises=lambda method: RemoteError("unknown_method", f"no method {method!r}"),
        expect_kwargs={"autostart": False},
    )
    monkeypatch.setattr(skills_mod, "DaemonClient", stub)
    assert cli.main(["skills"]) == 1
    error = capsys.readouterr().err
    assert "lacks skills support" in error
    assert "theater restart" in error


def test_skills_preserves_other_daemon_errors(monkeypatch):
    stub = daemon_stub(raises=lambda _method: RemoteError("internal", "registry unavailable"))
    monkeypatch.setattr(skills_mod, "DaemonClient", stub)
    with pytest.raises(RemoteError, match="registry unavailable"):
        cli.cmd_skills(parse("skills"))
