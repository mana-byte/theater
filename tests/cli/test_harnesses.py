"""`theater harnesses`, `models` and `discover`: listing, diagnostics and model discovery."""

from __future__ import annotations

import json

import pytest

from tests.support.cli_fakes import parse
from theater import cli, paths
from theater.cli.commands import introspection as introspection_mod
from theater.protocol import RemoteError


def test_harnesses_lists_every_registered_adapter(monkeypatch, capsys):
    monkeypatch.setattr(cli.shutil, "which", lambda binary: f"/usr/bin/{binary}")
    assert cli.cmd_harnesses(parse("harnesses")) == 0
    out = capsys.readouterr().out
    for name in cli.HARNESSES:
        assert name in out
    assert "not on PATH" not in out


def test_harnesses_says_which_ones_are_missing(monkeypatch, capsys):
    """Naming an uninstalled harness is the whole point: spawn will refuse it."""
    monkeypatch.setattr(cli.shutil, "which", lambda binary: None)
    cli.cmd_harnesses(parse("harnesses"))
    out = capsys.readouterr().out
    assert "not on PATH" in out
    for name in cli.HARNESSES:
        assert name in out


def test_harnesses_json_reports_install_state(monkeypatch, capsys):
    monkeypatch.setattr(cli.shutil, "which", lambda binary: None)
    cli.cmd_harnesses(parse("harnesses", "--json"))
    rows = json.loads(capsys.readouterr().out)
    assert {r["name"] for r in rows} == set(cli.HARNESSES)
    assert all(r["installed"] is False and r["path"] is None for r in rows)
    assert all(r["icon"] for r in rows)
    assert all(r["runtime"] == {"state": "inactive", "participants": []} for r in rows)
    assert all("manifest_path" in r and "channels" in r for r in rows)


def test_harness_diagnostics_prints_primary_once_after_json_round_trip():
    primary = {
        "id": "transcript",
        "kind": "transcript",
        "availability": "declared",
        "capabilities": [],
    }
    row = json.loads(
        json.dumps(
            {
                "manifest_api_version": 1,
                "manifest_path": "/tmp/acme/manifest.py",
                "primary_channel": primary,
                "channels": [primary],
            }
        )
    )

    lines = introspection_mod._harness_diagnostics(row)

    assert lines.count("primary transcript/transcript") == 1
    assert not any(line.startswith("channel transcript/transcript") for line in lines)


def test_harness_diagnostics_prints_runtime_success_and_latest_diagnostic(monkeypatch):
    monkeypatch.setattr(introspection_mod.time, "time", lambda: 101.25)
    lines = introspection_mod._harness_diagnostics(
        {
            "manifest_api_version": 1,
            "manifest_path": "/tmp/acme/manifest.py",
            "runtime": {
                "state": "active",
                "participants": [
                    {
                        "participant_id": "p1",
                        "channels": [
                            {
                                "id": "primary",
                                "state": "failed",
                                "diagnostics": [
                                    "older diagnostic",
                                    "primary read failed (ValueError)",
                                ],
                                "dropped": 2,
                                "accepted": 3,
                                "last_success_at": 1.25,
                            }
                        ],
                    }
                ],
            },
        }
    )

    assert lines[-1] == (
        "runtime p1: primary failed dropped=2 accepted=3 last_success=1m ago "
        "diagnostic=primary read failed (ValueError)"
    )


def test_harnesses_never_starts_a_daemon(monkeypatch, capsys):
    """It answers 'what can I spawn', so it must work before anything is up.

    It does ask a *running* daemon — that one is authoritative, since it holds
    the config as of its own start. But autostart is off, so the question is
    never the thing that launches one.
    """

    def explode(*a, **k):
        raise AssertionError("cmd_harnesses started a daemon")

    monkeypatch.setattr(introspection_mod, "call_sync", explode)
    monkeypatch.setattr(introspection_mod.DaemonClient, "_start_daemon", explode)
    assert cli.cmd_harnesses(parse("harnesses")) == 0
    assert "no daemon running" in capsys.readouterr().out


def test_harnesses_prefers_the_running_daemons_answer(monkeypatch, capsys):
    """The daemon may know harnesses this process cannot import."""

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def call(self, method, **params):
            assert method == "harnesses"
            return [
                {
                    "name": "codex",
                    "icon": "◇",
                    "binary": "codex",
                    "installed": True,
                    "path": "/usr/bin/codex",
                }
            ]

    monkeypatch.setattr(introspection_mod, "DaemonClient", FakeClient)
    assert cli.cmd_harnesses(parse("harnesses")) == 0
    out = capsys.readouterr().out
    assert "codex" in out
    assert "no daemon running" not in out
    assert "vibe" not in out


def test_harnesses_icon_column_pads_by_display_width(monkeypatch, capsys):
    """An icon of base-plus-combining (2 codepoints, 1 cell) must produce the
    same column offset as a single-codepoint icon (1 cell).  Padding by
    codepoint count — ``f"{icon:<2}"`` — pads the combining sequence to 2
    codepoints (no spaces), shifting every column after it left by one."""

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def call(self, method, **params):
            assert method == "harnesses"
            return [
                {
                    "name": "aaa",
                    "icon": "\u273b",
                    "binary": "aaa",
                    "installed": True,
                    "path": "/usr/bin/aaa",
                    "source": "shipped",
                },
                {
                    "name": "bbb",
                    "icon": "e\u0301",
                    "binary": "bbb",
                    "installed": True,
                    "path": "/usr/bin/bbb",
                    "source": "shipped",
                },
            ]

    monkeypatch.setattr(introspection_mod, "DaemonClient", FakeClient)
    assert cli.cmd_harnesses(parse("harnesses")) == 0
    out = capsys.readouterr().out
    lines = out.splitlines()
    row_aaa = lines[1]
    row_bbb = lines[2]

    # Literal expectations, derived by hand — not from pad_to_width.
    # Icon column is 2 display cells + 1 separator space.
    # "✻" is 1 cell → 1 padding space → prefix is "✻ " + separator = "✻  ".
    # "e\u0301" is 1 cell → 1 padding space → prefix is "e\u0301 " + separator = "e\u0301  ".
    assert row_aaa.startswith("\u273b  aaa")
    assert row_bbb.startswith("e\u0301  bbb")

    # The NAME column is 10 cells wide.  Both rows must have "shipped" at
    # the same codepoint offset relative to the start of the row, because
    # the icon prefix occupies the same number of *display cells* even
    # though it occupies a different number of codepoints.  The prefix
    # "✻  " is 3 codepoints; the prefix "e\u0301  " is 4 codepoints.  NAME
    # is 10 + 1 separator = 11, so SOURCE starts at codepoint offset 14
    # in row_aaa and 15 in row_bbb — but both show "shipped" at the same
    # *display* column.
    assert row_aaa[3 + 10 + 1 : 3 + 10 + 1 + 7] == "shipped"
    assert row_bbb[4 + 10 + 1 : 4 + 10 + 1 + 7] == "shipped"

    # The two prefixes have the same display width (3 cells each) even
    # though they differ in codepoint count (3 vs 4).
    from theater.formatting import display_width

    assert display_width(row_aaa[: row_aaa.index("aaa")]) == 3
    assert display_width(row_bbb[: row_bbb.index("bbb")]) == 3


def test_harnesses_falls_back_when_the_daemon_predates_the_method(monkeypatch, capsys):
    """Version skew: a daemon that old necessarily has the built-in registry."""

    class OldDaemon:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def call(self, method, **params):
            raise RemoteError("unknown_method", f"no method {method!r}")

    monkeypatch.setattr(introspection_mod, "DaemonClient", OldDaemon)
    assert cli.cmd_harnesses(parse("harnesses")) == 0
    out = capsys.readouterr().out
    assert "predates this command" in out
    for name in cli.HARNESSES:
        assert name in out


def test_a_real_daemon_error_is_not_papered_over(monkeypatch):
    """Any other failure must surface, not become a plausible-looking list."""

    class Broken:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def call(self, method, **params):
            raise RemoteError("internal", "the registry blew up")

    monkeypatch.setattr(introspection_mod, "DaemonClient", Broken)
    with pytest.raises(RemoteError):
        cli.cmd_harnesses(parse("harnesses"))


def write_config(text: str) -> None:
    paths.config_path().write_text(text, encoding="utf-8")


def discover(monkeypatch, harness: str, result, *extra):
    """Run `models --discover <harness>` with that adapter answering `result`.

    An exception as `result` is raised instead of returned, which is how the
    "cannot be asked" path is reached.

    Called directly rather than through `main`, because `main` rebuilds the
    registry first and `loading.scan` re-imports the plugin manifests — so neither
    the instance nor the class present at patch time is the one the command
    would end up calling. The real adapters have to be kept out of this: `vibe`
    reads ~/.vibe/config.toml, so unpatched these tests would assert against
    whatever models the developer running the suite happens to have.
    """

    def answer():
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(cli.HARNESSES[harness], "discover_models", answer)
    return cli.cmd_models(parse("models", "--discover", harness, *extra))


def test_models_lists_every_harness_not_only_the_configured_ones(capsys):
    """The unlisted ones are the point: those are what `--model` refuses."""
    write_config('[models]\nvibe = ["big"]\n')
    assert cli.main(["models"]) == 0
    out = capsys.readouterr().out
    assert "vibe" in out
    assert "big" in out
    assert "claude" in out
    assert "--model refused" in out


def test_models_says_how_to_get_started(capsys):
    """An empty allowlist refuses every --model, so the on-ramp must be visible."""
    assert cli.main(["models"]) == 0
    assert "--discover" in capsys.readouterr().out


def test_models_json_is_the_configured_mapping(capsys):
    write_config('[models]\nvibe = ["big", "small"]\n')
    assert cli.main(["models", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"vibe": ["big", "small"]}


def test_discover_prints_a_block_naming_the_harness(monkeypatch, capsys):
    assert discover(monkeypatch, "vibe", ["big", "small"]) == 0
    out = capsys.readouterr().out
    assert "[models]" in out
    assert "vibe = [" in out
    assert '"big",' in out


def pasted(capsys) -> str:
    """What a human would copy: the block, without the explanatory comments."""
    return "\n".join(
        line for line in capsys.readouterr().out.splitlines() if not line.startswith("#")
    )


def test_a_quoted_model_name_survives_the_round_trip(monkeypatch, capsys):
    """Names are quoted by a real encoder, not by string concatenation."""
    assert discover(monkeypatch, "vibe", ['weird"name']) == 0
    write_config(pasted(capsys))
    assert cli.main(["models", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"vibe": ['weird"name']}


def test_discover_json_is_the_found_list(monkeypatch, capsys):
    assert discover(monkeypatch, "vibe", ["big"], "--json") == 0
    assert json.loads(capsys.readouterr().out) == {
        "harness": "vibe",
        "models": ["big"],
    }


def test_discover_on_a_cli_that_cannot_be_asked_fails_helpfully(monkeypatch, capsys):
    problem = NotImplementedError("claude has no list")
    assert discover(monkeypatch, "claude", problem) == 1
    err = capsys.readouterr().err
    assert "claude has no list" in err
    # The manual route still works, and is the whole of the fix.
    assert "by hand" in err


def test_discover_that_finds_nothing_is_not_the_same_failure(monkeypatch, capsys):
    """Asked and answered: none. Usually an unauthenticated provider."""
    assert discover(monkeypatch, "vibe", []) == 1
    err = capsys.readouterr().err
    assert "no models" in err
    assert "by hand" not in err


def test_discover_never_prints_a_block_it_could_not_fill(monkeypatch, capsys):
    """An empty [models] block would read as an answer."""
    discover(monkeypatch, "vibe", [])
    assert capsys.readouterr().out == ""


def test_discover_rejects_an_unknown_harness(capsys):
    assert cli.main(["models", "--discover", "nope"]) == 1
    assert "unknown harness" in capsys.readouterr().err


def test_discover_accepts_an_alias_for_the_harness_name(monkeypatch, capsys):
    """Same spelling rules as `spawn`; the name came from the same head."""
    monkeypatch.setattr(cli.HARNESSES["claude"], "discover_models", lambda: ["big"])
    assert cli.cmd_models(parse("models", "--discover", "claude-code")) == 0
    assert "claude = [" in capsys.readouterr().out
