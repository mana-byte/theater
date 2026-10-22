"""CLI command modules and dispatch map."""

from __future__ import annotations

from argparse import Namespace
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum, auto

from theater.cli.commands.bus import cmd_bus
from theater.cli.commands.controls import (
    cmd_controls,
    cmd_interrupt,
    cmd_queue,
    cmd_settings,
    cmd_steer,
)
from theater.cli.commands.identity import (
    cmd_bind,
    cmd_candidates,
    cmd_claude_receipt,
    cmd_harness_event,
    cmd_transcript_receipt,
)
from theater.cli.commands.introspection import (
    cmd_config,
    cmd_harnesses,
    cmd_models,
    cmd_plugins,
    cmd_stats,
)
from theater.cli.commands.launch import cmd_launch as cmd_launch
from theater.cli.commands.maintenance import (
    cmd_gc,
    cmd_restart,
    cmd_stop,
)
from theater.cli.commands.management import (
    cmd_control_transfer,
    cmd_providers,
    cmd_workspaces,
)
from theater.cli.commands.participants import (
    _spawn_harness as _spawn_harness,
)
from theater.cli.commands.participants import (
    cmd_adopt,
    cmd_kill,
    cmd_ls,
    cmd_name,
    cmd_spawn,
)
from theater.cli.commands.plugin import cmd_plugin_call
from theater.cli.commands.process import (
    cmd_daemon,
    cmd_mcp,
    cmd_regie,
)
from theater.cli.commands.reload import cmd_reload
from theater.cli.commands.skills import cmd_skills
from theater.constants.cli import TRANSCRIPT_RECEIPT_COMMAND


class Initialization(Enum):
    NONE = auto()
    HOME = auto()
    HARNESSES = auto()


@dataclass(frozen=True, slots=True)
class CommandSpec:
    handler: Callable[[Namespace], int]
    initialization: Initialization


COMMAND_SPECS = {
    "daemon": CommandSpec(cmd_daemon, Initialization.HOME),
    "mcp": CommandSpec(cmd_mcp, Initialization.HOME),
    "plugin": CommandSpec(cmd_plugin_call, Initialization.HOME),
    TRANSCRIPT_RECEIPT_COMMAND: CommandSpec(cmd_transcript_receipt, Initialization.HARNESSES),
    "claude-receipt": CommandSpec(cmd_claude_receipt, Initialization.HARNESSES),
    "harness-event": CommandSpec(cmd_harness_event, Initialization.HOME),
    "ls": CommandSpec(cmd_ls, Initialization.HARNESSES),
    "bus": CommandSpec(cmd_bus, Initialization.HARNESSES),
    "spawn": CommandSpec(cmd_spawn, Initialization.HARNESSES),
    "steer": CommandSpec(cmd_steer, Initialization.HARNESSES),
    "queue": CommandSpec(cmd_queue, Initialization.HARNESSES),
    "settings": CommandSpec(cmd_settings, Initialization.HARNESSES),
    "controls": CommandSpec(cmd_controls, Initialization.HARNESSES),
    "interrupt": CommandSpec(cmd_interrupt, Initialization.HARNESSES),
    "kill": CommandSpec(cmd_kill, Initialization.HARNESSES),
    "name": CommandSpec(cmd_name, Initialization.HARNESSES),
    "candidates": CommandSpec(cmd_candidates, Initialization.HARNESSES),
    "bind": CommandSpec(cmd_bind, Initialization.HARNESSES),
    "adopt": CommandSpec(cmd_adopt, Initialization.HARNESSES),
    "harnesses": CommandSpec(cmd_harnesses, Initialization.HARNESSES),
    "plugins": CommandSpec(cmd_plugins, Initialization.HOME),
    "skills": CommandSpec(cmd_skills, Initialization.HOME),
    "stats": CommandSpec(cmd_stats, Initialization.HARNESSES),
    "gc": CommandSpec(cmd_gc, Initialization.HARNESSES),
    "config": CommandSpec(cmd_config, Initialization.HOME),
    "models": CommandSpec(cmd_models, Initialization.HARNESSES),
    "providers": CommandSpec(cmd_providers, Initialization.HARNESSES),
    "workspaces": CommandSpec(cmd_workspaces, Initialization.HARNESSES),
    "control-transfer": CommandSpec(cmd_control_transfer, Initialization.HARNESSES),
    "regie": CommandSpec(cmd_regie, Initialization.NONE),
    "stop": CommandSpec(cmd_stop, Initialization.HARNESSES),
    "restart": CommandSpec(cmd_restart, Initialization.HARNESSES),
    # The daemon reads and validates its own configuration during reload.
    "reload": CommandSpec(cmd_reload, Initialization.HOME),
}

# Keep the existing callable dispatch map available to direct callers and test overrides.
COMMANDS = {name: spec.handler for name, spec in COMMAND_SPECS.items()}
