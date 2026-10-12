"""`theater reload`: hot policy keys move in place; any other edit refuses the whole file."""

from __future__ import annotations

import json

import pytest

from theater import cli, paths
from theater import client as client_mod
from theater import config as cfg
from theater.cli.commands import introspection
from theater.cli.commands import reload as reload_cmd
from theater.cli.commands.reload import DaemonConfigStatus
from theater.config.reload import compare
from theater.protocol import RemoteError

_PATH = "/tmp/theater-home/config.toml"


def write(text: str) -> None:
    paths.config_path().write_text(text, encoding="utf-8")


def answer(**overrides) -> dict:
    base = {
        "path": _PATH,
        "dry_run": False,
        "applied": False,
        "changed": [],
        "restart_required": [],
        "outside_daemon": [],
    }
    return {**base, **overrides}


async def spawn_refusal(client, model: str) -> str:
    """The code the spawn gates answer with; no provider is registered, so none succeeds."""
    with pytest.raises(RemoteError) as refused:
        await client.call(
            "spawn", harness="vibe", prompt="hi", approval="manual", cwd="/tmp", model=model
        )
    return refused.value.code


def test_compare_names_each_difference_by_what_applying_it_needs():
    defaults = cfg.load()
    write("[rails]\nbudget = 20\n")  # the default, written down: provenance is not a change
    assert not compare(defaults, cfg.load()).any
    write(
        "[rails]\nbudget = 5\n"
        '[models]\nvibe = ["small"]\n'
        '[terminals]\ndefault_provider = "elsewhere"\n'
        '[theater]\nfavourite = "vibe"\n'
        "[observability]\nmcp_timing = true\n"
    )
    changes = compare(defaults, cfg.load())
    assert changes.hot == ("models.vibe", "rails.budget")
    assert changes.restart == ("terminals.default_provider",)
    assert changes.outside_daemon == ("observability.mcp_timing", "theater.favourite")


def test_a_changed_plugin_option_is_named_down_to_its_leaf():
    """Lists stay one value, TOML types stay apart, and a quoted dotted key never collides."""
    write(
        "[mcp.plugins.alerts]\n"
        'channels = ["ops", "dev"]\n'
        "retries = 1\n"
        '"x.y" = 1\n'
        'token = { env = "ALERTS_A" }\n'
        "[mcp.plugins.quiet]\n"
    )
    before = cfg.load()
    write(
        "[mcp.plugins.alerts]\n"
        'channels = ["ops", "eng"]\n'
        "retries = true\n"
        "x = { y = 1 }\n"
        'token = { env = "ALERTS_B" }\n'
    )
    changes = compare(before, cfg.load())
    assert changes.restart == (
        "mcp.plugins.alerts.channels",
        "mcp.plugins.alerts.retries",
        "mcp.plugins.alerts.token.env",
        "mcp.plugins.alerts.x.y",
        'mcp.plugins.alerts."x.y"',
        "mcp.plugins.quiet",
    )
    assert changes.hot == ()
    assert changes.outside_daemon == ()


async def test_a_hot_reload_moves_the_next_policy_check_on_the_same_socket(
    daemon, client, available_harness_binaries
):
    socket = client._writer
    assert await spawn_refusal(client, "small") == "model_not_allowed"
    write('[models]\nvibe = ["small"]\n[rails]\nbudget = 5\n')

    preview = await client.call("config.reload", dry_run=True)
    assert preview["applied"] is False
    assert preview["changed"] == ["models.vibe", "rails.budget"]
    assert await spawn_refusal(client, "small") == "model_not_allowed"

    result = await client.call("config.reload")
    assert result == {
        "path": str(paths.config_path()),
        "dry_run": False,
        "applied": True,
        "changed": ["models.vibe", "rails.budget"],
        "restart_required": [],
        "outside_daemon": [],
    }
    assert await spawn_refusal(client, "small") == "provider_unavailable"
    assert daemon.config.rails.budget == 5
    assert client._writer is socket


async def test_one_restart_only_change_refuses_the_whole_file(
    daemon, client, available_harness_binaries
):
    before = daemon.config
    write(
        '[models]\nvibe = ["small"]\n'
        '[terminals]\ndefault_provider = "elsewhere"\n'
        "[observer]\npoll_interval = 0.5\n"
    )
    result = await client.call("config.reload")
    assert result["applied"] is False
    assert result["changed"] == ["models.vibe"]
    assert result["restart_required"] == ["observer.poll_interval", "terminals.default_provider"]
    assert daemon.config is before
    assert await spawn_refusal(client, "small") == "model_not_allowed"


async def test_a_malformed_file_keeps_the_running_settings(daemon, client):
    before = daemon.config
    write("[rails\nbudget = 5\n")
    with pytest.raises(RemoteError) as refused:
        await client.call("config.reload")
    assert refused.value.code == "bad_request"
    assert "not valid TOML" in refused.value.message
    assert "kept its current settings" in refused.value.message
    assert daemon.config is before

    write("[rails]\nbudget = 5\n")
    assert (await client.call("config.reload"))["applied"] is True
    assert daemon.config.rails.budget == 5


async def test_settings_the_daemon_never_reads_do_not_block_a_reload(daemon, client):
    write('[theater]\nfavourite = "vibe"\n[observability]\nmcp_timing = true\n')
    result = await client.call("config.reload")
    assert result["applied"] is True
    assert result["changed"] == []
    assert result["restart_required"] == []
    assert result["outside_daemon"] == ["observability.mcp_timing", "theater.favourite"]
    assert (await client.call("config.reload"))["applied"] is False


@pytest.mark.parametrize(
    ("argv", "reply", "code", "expected"),
    [
        (["reload"], answer(applied=True, changed=["models.vibe"]), 0, "reloaded"),
        (["reload"], answer(), 0, "already matches"),
        (["reload", "--dry-run"], answer(dry_run=True, changed=["rails.budget"]), 0, "would"),
        (
            ["reload"],
            answer(changed=["models.vibe"], restart_required=["terminals.default_provider"]),
            1,
            "only `theater restart` applies: terminals.default_provider",
        ),
        (
            ["reload"],
            answer(applied=True, outside_daemon=["theater.favourite"]),
            0,
            "theater.favourite (read by `theater spawn` when no harness is named",
        ),
        (["reload"], RemoteError("bad_request", "not valid TOML — kept"), 1, "not valid TOML"),
        (["reload"], RemoteError("unknown_method", "no config.reload"), 1, "theater restart"),
    ],
)
def test_reload_cli_exit_codes_and_wording(monkeypatch, capsys, argv, reply, code, expected):
    asked = []

    def request(*, dry_run: bool) -> dict:
        asked.append(dry_run)
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(reload_cmd, "request_reload", request)
    assert cli.main(argv) == code
    captured = capsys.readouterr()
    assert expected in captured.out + captured.err
    assert asked == ["--dry-run" in argv]


def test_reload_needs_a_running_daemon_and_never_starts_one(monkeypatch, capsys):
    started = []
    monkeypatch.setattr(client_mod.subprocess, "Popen", lambda *a, **k: started.append(a))
    assert cli.main(["reload"]) == 1
    assert "no daemon running" in capsys.readouterr().err
    assert started == []


def test_reload_leaves_validation_to_the_daemon(monkeypatch):
    """No local harness install: a file this process cannot read must not stop the request."""
    write("[rails\n")

    def request(*, dry_run: bool) -> dict:
        return answer(dry_run=dry_run)

    monkeypatch.setattr(reload_cmd, "request_reload", request)
    assert cli.main(["reload"]) == 0


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (None, "running daemon: none"),
        (DaemonConfigStatus("match", answer()), "running daemon: matches this file"),
        (
            DaemonConfigStatus("reload", answer(changed=["rails.budget"])),
            "`theater reload` applies rails.budget",
        ),
        (
            DaemonConfigStatus(
                "restart",
                answer(changed=["models.vibe"], restart_required=["observer.poll_interval"]),
            ),
            "`theater restart` needed for observer.poll_interval",
        ),
        (DaemonConfigStatus("old"), "predates `theater reload`"),
    ],
)
def test_config_reports_how_the_running_daemon_compares(monkeypatch, capsys, status, expected):
    if status is not None:
        monkeypatch.setattr(introspection, "daemon_config_status", lambda: status)
    assert cli.main(["config"]) == 0
    assert expected in capsys.readouterr().out
    assert cli.main(["config", "--json"]) == 0
    daemon = json.loads(capsys.readouterr().out)["daemon"]
    assert daemon["status"] == ("absent" if status is None else status.state)
