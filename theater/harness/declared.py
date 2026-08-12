"""A harness described in `config.toml` instead of written in Python.

What is data and what is not
----------------------------
Of the six methods on `Harness`, two are pure data — how to launch, and what a
bare prompt looks like on screen — and the user can supply both. The other four
read a transcript, and a transcript format is not data: locating the file for a
session, extracting the harness's session id, turning a record into events, and
finding sub-agents are each a small program. So a declared harness implements
them as "nothing found", and everything above it copes:

    find_transcript   -> None      never observed through a file
    session_id        -> None      no harness-native id to match against
    parse             -> []        no events on the bus from the transcript
    native_children   -> []        sub-agents it spawns itself stay invisible

The consequence is not cosmetic. `parse` is what produces `turn_end`, and
`turn_end` is what finishes a job — without it `theater_send` would accept a
prompt and hang the caller's `await_sessions` forever. That is a failure this
project has already shipped once, so it is not left to chance: the observer runs
a different loop for these harnesses and derives the end of a turn from the
rendered screen. See `daemon/observer.py`, `_watch_screen`.

Anyone who needs real parsing writes a plugin: `docs/harness-plugins.md`.

Templates are substitution, not str.format
------------------------------------------
`mcp_file` contents are typically JSON, and `{"mcpServers": …}` is not a valid
format string — `str.format` would raise on the first brace. So placeholders are
replaced literally and every other brace is left exactly as written:

    {id}           participant id, the value that makes the MCP server ours
    {prompt}       the initial prompt
    {binary}       this harness's executable
    {theater}      absolute path to the theater binary (see theater_binary)
    {config_path}  where `mcp_file` will be written

Argv order is fixed rather than templated:

    binary  approval flags  mcp_argv  mcp_file_argv  argv

which is the order both built-in harnesses use, and the only one where flags
precede the positional prompt. An argv element that renders to the empty string
is dropped, so `["{prompt}"]` with no prompt launches an interactive session
rather than passing an empty argument.
"""

from __future__ import annotations

from pathlib import Path

from theater.config import HarnessSpec
from theater.harness.base import (
    APPROVALS,
    Event,
    Harness,
    LaunchPlan,
    NativeChild,
    last_screen_line,
    theater_binary,
)
from theater.models import BadRequest


def render(template: str, values: dict[str, str]) -> str:
    """Substitute `{name}` placeholders, leaving every other brace alone."""
    out = template
    for key, value in values.items():
        out = out.replace("{" + key + "}", value)
    return out


class DeclaredHarness(Harness):
    """The `Harness` interface backed by a `HarnessSpec` from config."""

    #: No transcript to tail. The observer reads this to pick its loop.
    has_transcript = False

    def __init__(self, name: str, spec: HarnessSpec):
        self.name = name
        self.spec = spec
        self.binary = spec.binary
        self.icon = spec.icon

    # ---- launching ------------------------------------------------------

    def _values(self, *, participant_id: str, prompt: str, config_path: Path) -> dict:
        return {
            "id": participant_id,
            "prompt": prompt,
            "binary": self.spec.binary,
            "theater": theater_binary(),
            "config_path": str(config_path),
        }

    def plan_launch(
        self,
        *,
        participant_id: str,
        prompt: str,
        config_path: Path,
        approval: str,
    ) -> LaunchPlan:
        if approval not in APPROVALS:
            raise BadRequest(
                f"approval must be one of {', '.join(APPROVALS)}, got {approval!r}"
            )
        values = self._values(
            participant_id=participant_id, prompt=prompt, config_path=config_path
        )

        argv = [self.spec.binary]
        for group in (
            self.spec.approvals[approval],
            self.spec.mcp_argv,
            self.spec.mcp_file_argv,
            self.spec.argv,
        ):
            for element in group:
                rendered = render(element, values)
                # An empty render means the placeholder had nothing in it —
                # an empty prompt, most often. Passing "" would be a real
                # argument the harness has to interpret.
                if rendered:
                    argv.append(rendered)

        env = {
            key: render(value, values)
            for key, value in {**self.spec.env, **self.spec.mcp_env}.items()
        }
        files = (
            {config_path: render(self.spec.mcp_file, values)}
            if self.spec.mcp_file is not None
            else {}
        )
        return LaunchPlan(argv=argv, env=env, files=files)

    # ---- observing ------------------------------------------------------

    def find_transcript(
        self,
        *,
        cwd: str,
        session_id: str | None = None,
        after: float | None = None,
    ) -> Path | None:
        """None, always: a declared harness has no declared transcript layout."""
        return None

    def session_id(self, transcript: Path) -> str | None:
        return None

    def parse(self, line: str, index: int, *, clip_text: bool = True) -> list[Event]:
        """No events. The observer watches the screen for this harness instead."""
        return []

    def native_children(self, transcript: Path) -> list[NativeChild]:
        return []

    def is_idle_screen(self, capture: str) -> bool:
        """Exact match against the declared prompts.

        Exact, not prefix: anything after the prompt is someone typing, which
        is presence rather than idleness. For a declared harness this decides
        when a turn ends, so a loose match would finish a job early and hand
        the caller a partial answer.
        """
        return last_screen_line(capture) in self.spec.idle_prompts
