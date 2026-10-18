"""Legacy Theater UI commands point operators to standalone Régie."""

from __future__ import annotations

from theater import cli
from theater.cli.commands import launch as launch_mod


def test_main_without_a_subcommand_never_dispatches_the_legacy_launcher(monkeypatch, capsys):
    monkeypatch.setitem(
        cli._COMMANDS,
        None,
        lambda _args: (_ for _ in ()).throw(AssertionError("legacy launcher was dispatched")),
    )

    assert cli.main([]) == 0
    assert "standalone `regie`" in capsys.readouterr().out


def test_legacy_launch_helper_only_gives_standalone_guidance(capsys):
    assert launch_mod.cmd_launch(object()) == 0
    assert "standalone `regie`" in capsys.readouterr().out
