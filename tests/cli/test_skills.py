"""`theater skills`: daemon wire, rendering and degraded daemons."""

from __future__ import annotations

import json

import pytest

from tests.support.cli_fakes import parse
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
    class FakeClient:
        def __init__(self, *, autostart):
            assert autostart is False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def call(self, method, **params):
            assert (method, params) == ("skills.list", {})
            return {
                "skills": [{"name": "alpha", "description": "An alpha skill.", "source": "user"}],
                "rejections": [
                    {
                        "name": "broken",
                        "path": "/tmp/skills/broken",
                        "error": "invalid frontmatter",
                    }
                ],
            }

    monkeypatch.setattr(skills_mod, "DaemonClient", FakeClient)
    assert cli.cmd_skills(parse("skills")) == 0
    out = capsys.readouterr().out
    assert "alpha" in out and "An alpha skill." in out
    assert "rejected skill packages" in out
    assert "/tmp/skills/broken" in out and "invalid frontmatter" in out


def test_skills_json_is_the_daemon_wire_object(monkeypatch, capsys):
    data = {"skills": [{"name": "alpha", "description": "A.", "source": "user"}], "rejections": []}

    class FakeClient:
        def __init__(self, **kwargs):
            assert kwargs == {"autostart": False}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def call(self, method, **params):
            return data

    monkeypatch.setattr(skills_mod, "DaemonClient", FakeClient)
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
    class NoDaemon:
        def __init__(self, **kwargs):
            assert kwargs == {"autostart": False}

        async def __aenter__(self):
            raise FileNotFoundError

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(skills_mod, "DaemonClient", NoDaemon)
    assert cli.main(["skills"]) == 1
    assert "no daemon running" in capsys.readouterr().err


def test_skills_reports_a_daemon_that_predates_the_rpc(monkeypatch, capsys):
    class OldDaemon:
        def __init__(self, **kwargs):
            assert kwargs == {"autostart": False}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def call(self, method, **params):
            raise RemoteError("unknown_method", f"no method {method!r}")

    monkeypatch.setattr(skills_mod, "DaemonClient", OldDaemon)
    assert cli.main(["skills"]) == 1
    error = capsys.readouterr().err
    assert "lacks skills support" in error
    assert "theater restart" in error


def test_skills_preserves_other_daemon_errors(monkeypatch):
    class BrokenDaemon:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def call(self, method, **params):
            raise RemoteError("internal", "registry unavailable")

    monkeypatch.setattr(skills_mod, "DaemonClient", BrokenDaemon)
    with pytest.raises(RemoteError, match="registry unavailable"):
        cli.cmd_skills(parse("skills"))
