"""CLI command modules and dispatch map."""

from __future__ import annotations

from theater.cli.commands.bus import cmd_bus
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
    cmd_stats,
)
from theater.cli.commands.launch import cmd_launch
from theater.cli.commands.maintenance import (
    cmd_gc,
    cmd_restart,
    cmd_stop,
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
from theater.cli.commands.skills import cmd_skills
from theater.constants.cli import TRANSCRIPT_RECEIPT_COMMAND

COMMANDS = {
    None: cmd_launch,
    "daemon": cmd_daemon,
    "mcp": cmd_mcp,
    "plugin": cmd_plugin_call,
    TRANSCRIPT_RECEIPT_COMMAND: cmd_transcript_receipt,
    "claude-receipt": cmd_claude_receipt,
    "harness-event": cmd_harness_event,
    "ls": cmd_ls,
    "bus": cmd_bus,
    "spawn": cmd_spawn,
    "kill": cmd_kill,
    "name": cmd_name,
    "candidates": cmd_candidates,
    "bind": cmd_bind,
    "adopt": cmd_adopt,
    "harnesses": cmd_harnesses,
    "skills": cmd_skills,
    "stats": cmd_stats,
    "gc": cmd_gc,
    "config": cmd_config,
    "models": cmd_models,
    "regie": cmd_regie,
    "stop": cmd_stop,
    "restart": cmd_restart,
}
