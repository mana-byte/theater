"""Grouped `theater --help` rendering.

Presentation only: groups decide where a command is listed, never whether it exists.
"""

from __future__ import annotations

import argparse

COMMAND_GROUPS: list[tuple[str, list[tuple[str | None, list[str]]]]] = [
    (
        "Daemon actions — talk to the daemon (most autostart it)",
        [
            ("Launch & participants", ["spawn", "ls", "kill", "name", "candidates", "bind"]),
            ("Control", ["steer", "queue", "settings", "controls", "interrupt"]),
            ("Observe", ["bus", "stats", "skills"]),
            ("Operator", ["providers", "workspaces", "control-transfer", "gc"]),
            ("Lifecycle", ["stop", "restart", "reload"]),
        ],
    ),
    (
        "Local — config, catalog, plugins (no daemon needed)",
        [(None, ["config", "harnesses", "models", "plugins"])],
    ),
    (
        "Process entry points — run a process, not a daemon client",
        [(None, ["daemon", "mcp", "plugin"])],
    ),
]

_HELP_COLUMN = 22


class GroupedHelpFormatter(argparse.RawDescriptionHelpFormatter):
    """Render the subcommand list as COMMAND_GROUPS instead of one flat block."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # Seed past the command column so the options section aligns at argparse's 24.
        self._action_max_length = _HELP_COLUMN

    def _format_action(self, action: argparse.Action) -> str:
        if not isinstance(action, argparse._SubParsersAction):
            return super()._format_action(action)
        helps = {choice.dest: choice.help or "" for choice in action._choices_actions}
        lines: list[str] = []
        for title, subgroups in COMMAND_GROUPS:
            lines.append(f"{title}:")
            for sub_title, names in subgroups:
                indent = "    " if sub_title is not None else "  "
                if sub_title is not None:
                    lines.append(f"  {sub_title}:")
                lines.extend(self._command_rows(names, helps, indent))
            lines.append("")
        return "\n".join(lines) + "\n"

    def _command_rows(self, names: list[str], helps: dict[str, str], indent: str) -> list[str]:
        width = max(self._width - _HELP_COLUMN, 11)
        rows: list[str] = []
        for name in names:
            first, *rest = self._split_lines(helps.get(name, ""), width) or [""]
            rows.append(f"{indent}{name}".ljust(_HELP_COLUMN - 2) + f"  {first}".rstrip())
            rows.extend(" " * _HELP_COLUMN + line for line in rest)
        return rows


def validate_command_groups(subparsers_action: argparse._SubParsersAction) -> None:
    """Fail at parser construction when a visible command is ungrouped or grouped twice."""
    hidden: set[str] = getattr(subparsers_action, "_hidden_names", set())
    registered = set(subparsers_action._name_parser_map)
    visible = registered - hidden
    grouped = [name for _, subgroups in COMMAND_GROUPS for _, names in subgroups for name in names]
    missing = sorted(visible - set(grouped))
    duplicated = sorted({name for name in grouped if grouped.count(name) > 1})
    unknown = sorted(set(grouped) - registered)
    if missing or duplicated or unknown:
        raise RuntimeError(
            "theater.cli.help.COMMAND_GROUPS is out of sync with the registered commands: "
            f"missing={missing}, duplicated={duplicated}, unknown={unknown}. "
            "List every visible command exactly once, or register it with hidden=True."
        )
