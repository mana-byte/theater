# Harness plugins

A harness plugin teaches Theater a CLI agent it has never heard of, in Python,
without touching the source tree. Drop a file in `$THEATER_HOME/harnesses/`
(default `~/.theater/harnesses/`), restart the daemon, and the harness is in the
registry: spawnable, observable, listed by `theater harnesses`, offered by the
régie palette.

## One mechanism, not two

Every adapter Theater can drive is a plugin, including the three it ships:
`claude`, `codex` and `vibe` live in `theater/harness/builtin/plugins/` and are
loaded by the same scanner that reads yours. There is no built-in tier and no
lighter-weight way to declare a harness in TOML.

There used to be. A `[harness.<name>]` table could describe how to launch a CLI
and what its idle prompt looked like, and that was enough for spawning, MCP
wiring, presence and an icon — everything except reading a transcript. It was
removed in v1.4 for two reasons.

The first is that the two halves are not comparable. A declared harness ended a
turn when its prompt reappeared on screen: a guess, confirmed across two polls,
but still a rendering. A plugin ends a turn when the transcript says so. Only a
plugin can put messages on the bus, answer `read_transcript`, or show native
sub-agents. Offering the cheap path first steered people toward the weaker half
of the system for harnesses that deserved the real one.

The second is that nothing that shipped used the extension point. The built-in
adapters were ordinary imports, so the plugin loader was exercised only by
tests. Now every shipped adapter goes through it on every run: the path you are
about to write on is the path Theater itself depends on.

A harness with no machine-readable transcript is still supported — set
`has_transcript = False` and the observer falls back to the screen for turn
boundaries. That is a property of the adapter, not a second kind of adapter.

The three shipped plugins are the best worked examples available. `vibe.py` is
the shortest, `codex.py` shows a harness whose MCP wiring goes on the command
line, and `claude.py` shows one that needs a config file written first.

## Where plugins live, and how they load

```
$THEATER_HOME/harnesses/
├── nova.py          loaded
├── _shared.py       skipped: leading underscore
└── .draft.py        skipped: leading dot
```

Every `*.py` in that directory, in filename order, must export a `Harness`
instance named `HARNESS`. Files beginning with `_` or `.` are skipped, which is
what makes a shared helper module possible next to the plugins that import it.

Loading is by path under a synthetic module name (`theater_harness_plugin_nova`),
not by putting the directory on `sys.path`. The difference matters: on
`sys.path`, a file you called `json.py` would shadow the standard library for
the whole daemon process, and the resulting failure would name neither your file
nor the plugin system. Under a prefixed name it collides with nothing — and your
plugin can `import json` and get the real one.

The directory is created empty by `theater daemon` on first run. Its existence
is how the extension point announces itself.

Plugins are read once, at start-up. After editing one:

```
theater restart
```

## The identity attributes

```python
class NovaHarness(Harness):
    name = "nova"                  # required
    binary = "nova"                # required
    icon = "◈"                     # one character
    aliases = ("nova-cli",)        # optional
    has_transcript = True          # default
```

`name` is the spawn key (`theater spawn nova …`) and the registry key. Lowercase
letters, digits, `-` and `_`, starting with a letter or digit.

`binary` is what is looked for on `PATH` to decide whether the harness is
installed, and what the unmanaged-pane sweep matches a pane's running command
against. It is not automatically argv[0] — your `plan_launch` decides that — but
it should be the same executable, or `theater harnesses` will lie.

`icon` is exactly one character. A single glyph, not an image: terminal image
protocols do not survive tmux. Width 1 so no listing reflows when a harness is
added, and preferably a codepoint a default font has — a Nerd Font private-use
glyph renders as an empty box for anyone who has not installed one.

`aliases` are other spellings that should resolve to `name` when an agent
reports its own harness at registration. An agent that calls itself `nova-cli`
with no alias registered is observed as nothing at all, forever. An alias that
already belongs to another harness is refused at load time rather than
silently reassigned.

`has_transcript` selects the observer's loop. Leave it `True` if `parse` works.
Set it `False` if your `find_transcript` will always return `None` — otherwise
the observer searches for a file that never appears, the participant never
produces an event, and every `theater_send` to it hangs.

## The interface, method by method

Six abstract methods. Two are about launching, four about observing.

### `plan_launch(*, participant_id, prompt, config_path, approval) -> LaunchPlan`

Describe how to start the harness. Pure — it must not write anything itself;
return the files you need written and the spawner writes them before the window
is created.

```python
LaunchPlan(
    argv=["nova", "--mcp-config", str(config_path), prompt],
    env={"NOVA_SOMETHING": "1"},
    files={config_path: json.dumps(server_config)},
)
```

`participant_id` is the twelve-character Theater id, and getting it into the
harness is the whole reason this method is not a template. The MCP server has to
come up already knowing which participant it belongs to, and the *only* channel
that survives is the server's own argv:

```
theater mcp --id <participant_id>
```

Not the environment. The MCP Python SDK does not pass the parent environment to
a stdio server: when a server config omits `env` it substitutes an allowlist of
six variables and drops everything else. So `THEATER_ID` in the pane environment
is visible to the harness process but *not* to the MCP server it spawns. Bake
the id into argv. Use `theater_binary()` to resolve the absolute path — a tmux
window does not inherit the daemon's `PATH`, so the bare name `theater` may not
be found.

Each harness has a different lever for getting that argv in place. Vibe reads
`VIBE_MCP_SERVERS` from the environment; Claude Code takes `--mcp-config=<path>`;
Codex takes `-c mcp_servers.theater.command=…` on the command line. Look for
yours before writing the method — it is the part most likely to be wrong.

`config_path` is a per-participant path under `$THEATER_HOME/mcp/` that Theater
has reserved for you. Use it as the key in `files` if your harness wants a config
file; ignore it entirely if it does not.

`approval` is one of `manual`, `edits`, `yolo`, and it is always passed — there
is no default anywhere in Theater, because the choice is the whole safety story
for a child nobody is watching. Map all three. Raise `BadRequest` for anything
else.

`prompt` may be empty, meaning "start interactive with nothing to do". Do not
append an empty string to argv; most CLIs treat it as a real, blank argument.

### `find_transcript(*, cwd, session_id=None, after=None) -> Path | None`

Locate the file this session writes, or `None` if it is not there yet. Called
repeatedly until it answers, so `None` means "not yet", not "never".

Note what is *not* a parameter: the tmux pane. No harness records the pane it
was launched from, so a pane cannot narrow the search. The usable keys are the
working directory, the harness's own session id once known, and a lower bound on
start time.

`after` is a floor on session start, set for participants Theater spawned and
whose creation time it therefore knows. It is `None` for adopted participants,
whose transcript predates Theater's first sight of them — so do not treat
`after=None` as zero and pick the newest file; treat it as "no floor" and match
on the working directory.

Returning `None` forever with `has_transcript = True` is the one silent failure
mode in this interface. Set `has_transcript = False` instead.

### `session_id(transcript) -> str | None`

The harness's own identifier for the session, read from the transcript or its
directory. Recorded on the participant so harness-native identifiers — which is
what sub-agent bookkeeping is written in — can be matched back to a Theater
participant later.

`None` is fine. It costs you native-child matching, nothing else.

### `parse(line, index, *, clip_text=True) -> list[Event]`

Turn one line of the transcript into zero or more normalized events. This is the
method that makes Theater cross-harness: nothing above the adapter ever sees
your format.

Returning `[]` is normal and common — the shipped harnesses skip bookkeeping
records that mean nothing to an observer. A malformed line must also return `[]`
rather than raise: the file is being appended to while you read it, and a torn
last line is an expected condition, not an error.

```python
Event(
    kind=EventKind.ASSISTANT,   # USER | ASSISTANT | TOOL_CALL | TOOL_RESULT | ERROR
    text=clip("..."),
    tool_name=None,
    ts=None,                    # None if the harness writes no timestamp
    turn_end=True,              # the agent stopped and is waiting for a human
    raw_index=index,
)
```

`turn_end` is the load-bearing field. It sets the participant to IDLE, and it
finishes the job that `theater_send` created. Get it wrong in one direction and
a caller waits forever; wrong in the other and it reads a partial answer. Find
the record that means "the model stopped": Claude Code has an explicit
`stop_reason`, Vibe has the *absence* of a `tool_calls` key on an assistant
record. Emit exactly one `turn_end` per turn, on the last event of that turn.

`ts` should be the timestamp the transcript recorded, or `None` if it records
none. Do not substitute the current time — the observer already stamps its own
observation time, and a stamped-on-read time is a different quantity from when
the event happened.

`clip_text` distinguishes the two callers. `True` (the default) means the events
are going on the bus, which is an activity feed, not an archive: clip with
`clip()` from `theater.harness`, since a single tool result is routinely 25 KB.
`False` means `read_transcript` is reading the full record back for an agent.
The helper `clipper(clip_text)` returns the right function; use it rather than
branching.

`index` is the zero-based record number. Pass it through as `raw_index`. Several
events may share one index — a Vibe assistant turn with three tool calls is four
events.

### `native_children(transcript) -> list[NativeChild]`

Sub-agents the harness spawned by itself, outside Theater's knowledge, read from
whatever bookkeeping it keeps. These are a second lineage edge: Theater did not
create them and cannot address them, but showing them in the tree is the
difference between an accurate picture and a misleading one.

`[]` is a perfectly good answer, and the right one if the harness has no
sub-agents or does not record them.

### `is_idle_screen(capture) -> bool`

Given `tmux capture-pane -p` output — the rendered pane as plain text — does the
screen show a bare prompt?

For a plugin with a working `parse`, this is a display hint: it produces the
AWAITING_INPUT status that tells a human "this agent is blocked on a permission
prompt". Tune it to accept false negatives and return `False` when unsure. A
false positive marks a working agent idle and hides activity from the régie.

The helper `last_screen_line(capture)` gives the bottom-most non-empty line,
stripped. Match it *exactly* against your prompt strings, not as a prefix:
anything after the prompt is a human typing, which is presence, not idleness.

## A complete plugin

`nova` is invented for this document — there is no such CLI. The shape is real;
the file paths and record format are not.

```python
"""Nova. Writes ~/.nova/sessions/<session-id>/log.jsonl."""

import json
from pathlib import Path

from theater.harness import (
    Event,
    EventKind,
    Harness,
    LaunchPlan,
    NativeChild,
    clipper,
    last_screen_line,
    theater_binary,
)
from theater.models import BadRequest

APPROVAL_FLAGS = {
    "manual": [],
    "edits": ["--auto-edit"],
    "yolo": ["--yes-to-everything"],
}

IDLE_PROMPTS = ("nova>", "nova> ")


class NovaHarness(Harness):
    name = "nova"
    binary = "nova"
    icon = "◈"
    aliases = ("nova-cli",)

    def __init__(self, root: Path | None = None):
        # Injectable so a test never touches the real ~/.nova.
        self.root = root or Path.home() / ".nova" / "sessions"

    # ---- launching ----------------------------------------------------

    def plan_launch(self, *, participant_id, prompt, config_path, approval):
        flags = APPROVAL_FLAGS.get(approval)
        if flags is None:
            raise BadRequest(f"unknown approval mode {approval!r}")

        config = {
            "mcpServers": {
                "theater": {
                    "command": theater_binary(),
                    "args": ["mcp", "--id", participant_id],
                }
            }
        }
        argv = [self.binary, *flags, "--mcp-config", str(config_path)]
        if prompt:
            argv.append(prompt)
        return LaunchPlan(
            argv=argv,
            files={config_path: json.dumps(config)},
        )

    # ---- observing ----------------------------------------------------

    def find_transcript(self, *, cwd, session_id=None, after=None):
        if not self.root.is_dir():
            return None
        if session_id:
            log = self.root / session_id / "log.jsonl"
            return log if log.exists() else None

        want = str(Path(cwd).resolve()) if cwd else None
        if want is None:
            return None
        for directory in sorted(self.root.iterdir(), reverse=True):
            log = directory / "log.jsonl"
            if not log.exists():
                continue
            if after is not None and directory.stat().st_mtime < after:
                continue
            if self._meta(directory).get("cwd") == want:
                return log
        return None

    def _meta(self, directory: Path) -> dict:
        try:
            data = json.loads((directory / "meta.json").read_text())
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def session_id(self, transcript):
        return transcript.parent.name

    def parse(self, line, index, *, clip_text=True):
        line = line.strip()
        if not line:
            return []
        try:
            record = json.loads(line)
        except ValueError:
            return []          # torn last line; the file is still being written
        if not isinstance(record, dict):
            return []

        clip = clipper(clip_text)
        role = record.get("role")

        if role == "user":
            return [
                Event(
                    kind=EventKind.USER,
                    text=clip(record.get("text")),
                    ts=record.get("time"),
                    raw_index=index,
                )
            ]
        if role != "assistant":
            return []          # bookkeeping record, nothing to show

        events = []
        if record.get("text"):
            events.append(
                Event(
                    kind=EventKind.ASSISTANT,
                    text=clip(record["text"]),
                    ts=record.get("time"),
                    raw_index=index,
                )
            )
        for call in record.get("tools") or []:
            events.append(
                Event(
                    kind=EventKind.TOOL_CALL,
                    tool_name=call.get("name"),
                    ts=record.get("time"),
                    raw_index=index,
                )
            )

        # The turn ends when Nova says so. Mark the last event, and make sure
        # there is one to mark even on a degenerate record.
        if record.get("done"):
            if not events:
                events.append(Event(kind=EventKind.ASSISTANT, raw_index=index))
            last = events[-1]
            events[-1] = Event(
                kind=last.kind,
                text=last.text,
                tool_name=last.tool_name,
                ts=last.ts,
                turn_end=True,
                raw_index=last.raw_index,
            )
        return events

    def native_children(self, transcript):
        entries = self._meta(transcript.parent).get("subagents") or []
        return [
            NativeChild(session_id=e["id"], agent=e.get("name"))
            for e in entries
            if isinstance(e, dict) and e.get("id")
        ]

    def is_idle_screen(self, capture):
        return last_screen_line(capture) in IDLE_PROMPTS


HARNESS = NovaHarness()
```

If your harness has no transcript at all, set `has_transcript = False` and the
observing methods collapse to four one-liners: the observer stops looking for a
file and reads the screen instead, and `is_idle_screen` becomes the signal that
a turn ended rather than a hint about a stuck agent.

## Precedence

The registry is rebuilt at every start-up in this order:

```
shipped plugins  →  local plugins
```

Later wins: name your plugin `vibe` and it takes over from the shipped Vibe
adapter. That is deliberate. Both are the same kind of file with the same
powers, so overriding one is the supported way to fix or extend it locally
without editing an installed package.

The asymmetry is in what a failure means. A shipped plugin that will not import
stops start-up — the install is broken, and the only way past it is `[harness]
disabled`, which is why that key matches the filename before the file is
imported. A local plugin that will not import is skipped with a warning and
shown as rejected by `theater harnesses`, because a file in your own directory
is yours to break and should not take the daemon down with it.

Two plugins defining the same name is an error naming both files. An alias
collision is an error naming the claimant and the current owner. Nothing here
resolves a conflict by load order, because "whichever file sorts first wins" is
not something anyone can debug from the symptom.

## When it goes wrong

Every failure below is reported with the file path in the message. For a
shipped plugin it stops start-up; for one of yours it is a warning, the harness
is absent from the registry, and `theater harnesses` lists it under rejected. A
plugin the user believes they installed but which is quietly absent — with
nothing anywhere saying so — is the defect this design exists to prevent.

| What you wrote | What you get |
|---|---|
| a file that raises on import | `…/nova.py: failed to import: ValueError(...)` |
| a syntax error | `…/nova.py: failed to import: SyntaxError(...)` |
| no `HARNESS` | `…/nova.py: defines no HARNESS. A plugin must end with HARNESS = MyHarness()` |
| `HARNESS = NovaHarness` | `…/nova.py: HARNESS is the class NovaHarness, not an instance of it` |
| `HARNESS = 3` | `…/nova.py: HARNESS is a int, which does not subclass theater.harness.Harness` |
| a missing abstract method | `…/nova.py: failed to import: TypeError("Can't instantiate abstract class …")` |
| `name = "My Nova"` | `…/nova.py: harness name 'My Nova' must be lowercase letters, digits, '-' or '_'` |
| `binary = ""` | `…/nova.py: harness 'nova' sets no binary to look for` |
| `icon = "<>"` | `…/nova.py: harness 'nova' has icon '<>'; it must be exactly one character` |
| an alias another harness owns | `…/nova.py claims alias 'mistral-vibe', which already resolves to 'vibe'` |

These surface wherever the registry is built: `theater daemon`, and every CLI
command including `theater config`. Check your plugin with the cheapest one:

```
theater harnesses
```

which either lists `nova` or tells you why it could not.

## Testing a plugin without spawning anything

Everything except `plan_launch`'s effect on a real CLI is testable offline, and
the launch plan itself is pure data you can assert on.

```python
from pathlib import Path
from theater import harness as registry

def test_it_loads(tmp_path):
    (tmp_path / "nova.py").write_text(PLUGIN_SOURCE)
    added = registry.install(Config(), plugin_dir=tmp_path)
    assert "nova" in added
    assert registry.HARNESSES["nova"].icon == "◈"
```

`install(config, plugin_dir=…)` is the seam: it rebuilds the registry from a
directory you choose, so nothing has to relocate `$THEATER_HOME`. It is
idempotent — calling it again rebuilds from scratch rather than accumulating —
and `install(Config())` restores the shipped set, which is how a test cleans up
after itself.

For the harness itself, instantiate it directly:

```python
h = NovaHarness(root=tmp_path)             # never the real ~/.nova

plan = h.plan_launch(
    participant_id="abc123def456",
    prompt="hello",
    config_path=tmp_path / "mcp.json",
    approval="manual",
)
assert "--mcp-config" in plan.argv
assert "abc123def456" in plan.files[tmp_path / "mcp.json"]

events = h.parse('{"role":"assistant","text":"hi","done":true}', 0)
assert [e.kind for e in events] == [EventKind.ASSISTANT]
assert events[0].turn_end

assert h.parse("{not json", 0) == []       # torn line, no exception
assert h.is_idle_screen("nova> ")
assert not h.is_idle_screen("nova> what is")
```

Take the constructor-injected root seriously. Every shipped adapter has one
for exactly this reason, and a test that reads the real home directory passes or
fails depending on what the developer did yesterday.

`tests/test_harness_plugins.py` in this repository is the loader's own test
suite and doubles as a worked example of the fixtures.

## Trust

A plugin is arbitrary Python executed by the daemon at the privileges of the
user who started it. There is no sandbox and no attempt at one — the same trust
level as `~/.bashrc` or a shell plugin, in a directory under `$THEATER_HOME`
where nothing else writes.

Read a plugin before you install it, exactly as you would a shell snippet from
the internet.
