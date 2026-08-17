# Harness plugins

A harness plugin teaches Theater a CLI agent it has never heard of, in Python,
without touching the source tree. Drop a file in `$THEATER_HOME/harnesses/`
(default `~/.theater/harnesses/`), restart the daemon, and the harness is in the
registry: spawnable, observable, listed by `theater harnesses`, offered by the
régie palette.

## One mechanism, not two

Every adapter Theater can drive is a plugin, including the four it ships:
`claude`, `codex`, `opencode` and `vibe` live in
`theater/harness/builtin/plugins/` and are loaded by the same scanner that reads
yours. There is no built-in tier and no
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

A harness with no machine-readable transcript is still supported — its observer
sets `has_transcript = False` and the daemon falls back to the screen for turn
boundaries. That is a property of the adapter, not a second kind of adapter.

The four shipped plugins are the best worked examples available. `vibe.py` is
the shortest, `codex.py` shows a harness whose MCP wiring goes on the command
line, `claude.py` shows one that needs a config file written first, and
`opencode.py` shows one that writes no transcript file at all and reads its own
SQLite event log instead.

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

## Two objects: a harness and its observer

A plugin answers two unrelated questions, and since v1.6 they are two classes.
*How do I start this CLI so that it comes up knowing its participant id* is the
`Harness`. *How do I tell what it is doing once it is running* is a
`HarnessObserver`, which the harness constructs and carries:

```python
class NovaHarness(Harness):
    name = "nova"                  # required
    binary = "nova"                # required
    icon = "◈"                     # one character
    aliases = ("nova-cli",)        # optional

    def __init__(self, root: Path | None = None):
        self.observer = NovaObserver(root=root)


class NovaObserver(TranscriptObserver):
    has_transcript = True          # default

    def __init__(self, root: Path | None = None):
        self.root = root or Path.home() / ".nova" / "sessions"


HARNESS = NovaHarness()
```

The split is not bookkeeping. `opencode.py` used to implement `find_transcript`,
`session_id`, `parse` and `native_children` purely to return nothing, because
its output is a shared SQLite database and none of those questions has an answer
for it — a plugin that must write four stubs to say "not applicable" is being
described by the wrong interface. It also fixes who talks to whom: the daemon's
reducer needs nothing from a harness except how to watch it, and it now holds
the observer rather than the harness.

Assigning `self.observer` is not optional. It is checked at load time rather
than declared abstract, because a property returning a value the constructor
already has is four lines of ceremony in every plugin; see the failure table
below for what forgetting it says.

### On the harness

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

### On the observer

`has_transcript` selects the daemon's watch loop, and the name is narrower than
the meaning: it asks whether your adapter can be observed by *reading* anything
at all. Leave it `True` if `parse` works, and also if you have no file but
override `open_source` to read some other store. Set it `False` only when there
is nothing to read — otherwise the daemon waits on a source that never produces,
the participant never produces an event, and every `theater_send` to it hangs.

Whatever locates the harness's output — a transcript root, a database path —
belongs on the observer, injected through its constructor, which is what keeps a
test away from the developer's real home directory. Nothing per-session belongs
there: one observer is shared by every session of its harness, and per-session
state lives on the `Source` it opens.

## The harness, method by method

One abstract method and one optional concrete one. Everything else on a harness
is the identity data above and the observer it carries.

### `plan_launch(*, participant_id, prompt, config_path, approval, model=None) -> LaunchPlan`

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

`model` is optional and defaults to `None`, meaning "the CLI picks". Accept it if
your CLI can be pointed at a specific model, and pass the string through
untouched — do not validate it against a list of names you know. Model namespaces
change faster than a plugin gets updated, and a name your CLI rejects fails
visibly in the pane, which is the right place for it.

Declaring the parameter is what opts you in. Theater inspects your signature and
only passes `model` when you have somewhere to put it, so a plugin written before
this option existed keeps working unchanged for every launch that does not ask
for a model. Ask such a plugin for one and the spawn is refused by name, before
anything is created — Theater will not quietly drop the caller's choice and start
the wrong model instead.

Use whichever lever the CLI actually offers; it does not have to be a flag. The
built-ins split both ways — `claude`, `codex`, and `opencode` take a flag, while
`vibe` has none and reads `VIBE_ACTIVE_MODEL` from the environment. If yours is
an environment variable, set it *unconditionally*, empty when no model was asked
for:

```python
env["NOVA_MODEL"] = model or ""
```

Environments are inherited and flags are not. Skip the empty case and an agent
you started on one model hands that model to every grandchild it spawns without
one.

### `discover_models() -> list[str]`

Optional. Model names your CLI reports it can run, for `theater models
--discover <harness>`, which prints them as a `[models]` block for the user to
paste into Theater's config and cut down.

Concrete on the base class, so omitting it costs you nothing: the inherited
version raises `NotImplementedError`, and the CLI reports that as "cannot be
asked" rather than as a broken plugin. Two of the four shipped adapters do
exactly that — neither `claude` nor `codex` offers a listing of any kind, and a
hand-written catalogue on their behalf would go stale in silence.

**This is an authoring aid and never a gate.** It is not consulted when a spawn
happens. What a spawn may name is the `[models]` allowlist in Theater's config,
which a human wrote; your job here is only to save them the typing. So it is
fine for the answer to be incomplete, out of date, or to include models the user
is not authenticated for.

Two failure shapes, and the distinction is the contract:

- **`NotImplementedError`** — there is no way to ask. No command, no config file
  to read, or the binary is not installed. Retrying will not help, and the CLI
  says so and points at writing the list by hand.
- **`[]`** — you asked and were told none. Usually a provider that is not logged
  in yet, which the CLI reports as such, because that one *is* worth retrying
  after logging in.

Never return a guess to paper over either. Turn anything that goes wrong while
asking into `NotImplementedError` with a message that says what was tried:

```python
def discover_models(self) -> list[str]:
    try:
        out = subprocess.check_output(
            [self.binary, "models"], text=True, timeout=20,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError as exc:
        raise NotImplementedError(f"{self.binary} is not on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise NotImplementedError(f"`{self.binary} models` timed out") from exc
    return [line.strip() for line in out.splitlines() if line.strip()]
```

A subprocess is not the only source. `vibe` has no listing command, so its
adapter reads the `[[models]]` tables out of `~/.vibe/config.toml` instead —
whatever answers the question without starting a session. Give any subprocess a
timeout: a human is waiting at a terminal, and hanging is worse than failing.

Return the names in the CLI's own spelling, in whatever order is most useful to
read, deduplicated. They go straight into a config file and then straight back
to your `plan_launch` as `model`, so a friendly alias here becomes a name that
is allowlisted and then rejected by the CLI.

## The observer, method by method

Four methods you will usually write and two that have defaults.
`find_transcript`, `session_id` and `parse` are abstract on
`TranscriptObserver`; `is_idle_screen` is abstract on every observer, because
the daemon needs it for two things reading cannot do — telling "blocked on a
permission prompt" apart from "thinking", and confirming a pane looks idle
before rescuing a job whose turn end was never seen. `native_children` defaults
to none. `open_source` defaults to tailing a file, and only a harness whose
output is not a file replaces it — see "When the output is not a file" below.

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
mode in this interface. If there is no file to find, you want one of the other
two shapes: subclass `HarnessObserver` and write a source, or set
`has_transcript = False`.

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

Spawned transcript-backed adapters need a real identity channel if their
transcripts live in a shared namespace. A cwd/time match is `heuristic`: Theater
may show it in `theater candidates`, but it will not attribute text, complete
turns, allow trusted resume, or accept sends on that evidence alone. Use
participant-isolated transcript storage, an exact launch/lifecycle receipt, or a
daemon-checkable process proof so the source can report `exact` or `proven`
ownership. If a trusted pin later disappears or a newer heuristic candidate
appears while the old pin is inert and the screen is working, Theater enters
`transcript_identity_lost` and waits for operator bind rather than repointing
your source.

`index` is the zero-based record number. Pass it through as `raw_index`. Several
events may share one index — a Vibe assistant turn with three tool calls is four
events.

### `native_children(transcript) -> list[NativeChild]`

Sub-agents the harness spawned by itself, outside Theater's knowledge, read from
whatever bookkeeping it keeps. These are a second lineage edge: Theater did not
create them and cannot address them, but showing them in the tree is the
difference between an accurate picture and a misleading one.

`[]` is a perfectly good answer, and the right one if the harness has no
sub-agents or does not record them — which is why it is the default and why you
can leave the method out entirely.

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

## When the output is not a file

Everything above assumes the harness appends to a transcript. Most do. If yours
writes to a database or offers only an event stream, subclass `HarnessObserver`
directly instead of `TranscriptObserver`, implement `open_source`, and the
byte-offset model gets out of your way:

```python
class NovaObserver(HarnessObserver):
    def open_source(self, *, cwd, session_id=None, after=None) -> Source:
        return NovaSource(cwd=cwd)

    def is_idle_screen(self, capture):
        return last_screen_line(capture) in IDLE_PROMPTS
```

That is the whole observer. `find_transcript`, `session_id` and `parse` are not
on this base class at all — they are how the *default* source is built, and a
plugin supplying its own source has no reason to mention them. Before v1.6 they
were abstract on every adapter and this plugin had to define three stubs to say
so; deleting those stubs is what the split bought.

A `Source` is a live view of one participant's output. Unlike the rest of the
interface it is an object with a lifetime, so it is the right place for a
connection or a subscription you need to hold open:

```python
class NovaSource(Source):
    async def read(self) -> Batch:       # required
    async def refresh(self) -> Batch:    # optional: re-check where to read from
    async def aclose(self) -> None:      # optional: release what you hold
```

`read` is polled and returns a `Batch`:

| field | meaning |
|---|---|
| `events` | normalized `Event`s, exactly as `parse` would produce |
| `progressed` | you consumed new input, even if it produced no events |
| `status` | an authoritative status, when you can actually tell |
| `attached` | an `Attachment`, the first time you start reading somewhere |
| `waiting` | there is nothing to read *from* yet |

Three things are worth getting right.

**`progressed` is not "produced events".** Bookkeeping records that parse to
nothing still mean the agent is alive. If that read as silence, the rescue timer
would fire mid-turn and hand a caller a half-finished answer. Report it. The
reverse is free — events are counted as progress whether you set the flag or
not.

**`status` is for sources that can ask.** Tailing an append-only file gives no
turn-end signal beyond what the records say, so the observer infers status from
silence. If your harness will tell you plainly that a session went idle, put it
here and the guessing is skipped for your participants. Leave it `None` and you
get the same inference everyone else does.

**Hold mutable records back.** A byte offset into an append-only file is a proof
that everything behind it is final. A cursor into a table is only a watermark:
rows behind it may still change. Emit a record when it is terminal, not while it
is still being written — the bus has no retraction.

What you do *not* implement is everything the observer does with a batch: status
transitions, job completion, the rescue path, dead detection, the awaiting-input
check. That policy is written once and it is where every observation bug in this
project has been. A source reports facts; it must not touch the registry, the
bus or the job manager.

One optional method is worth implementing: `history`.

```python
async def history(self, *, last_n: int) -> History:
```

`read` is a tail — it answers "what happened since I last looked". `history`
answers "what has this session said, from the beginning", and it is what backs
the `read_transcript` tool, which exists because the bus clips long replies and
an agent sometimes needs the whole thing. The default implementation re-reads
the file with clipping off; a source over a database has to write its own. Skip
it and callers get an empty transcript with no error — the one place a custom
source silently loses a feature.

Two rules. Return the *newest* `last_n` events, `0` meaning all. And do not clip
text: clipping is the caller's job, and this is the path a caller takes
precisely because the clipped copy was not enough.

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
    TranscriptObserver,
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
        # Nothing else to keep: the harness starts the CLI, the observer reads
        # it, and only the reading needs to know where ~/.nova is.
        self.observer = NovaObserver(root=root)

    def plan_launch(self, *, participant_id, prompt, config_path, approval, model=None):
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
        if model:
            # Passed through as given. Nova owns its namespace, not us.
            argv += ["--model", model]
        if prompt:
            argv.append(prompt)
        return LaunchPlan(
            argv=argv,
            files={config_path: json.dumps(config)},
        )


class NovaObserver(TranscriptObserver):
    def __init__(self, root: Path | None = None):
        # Injectable so a test never touches the real ~/.nova.
        self.root = root or Path.home() / ".nova" / "sessions"

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

If your harness writes no transcript file, you have two options, and both change
only the observer — `NovaHarness` above is already finished either way. If it
keeps its history somewhere else — a database, a socket — subclass
`HarnessObserver`, implement `open_source`, and keep `has_transcript = True`;
`opencode.py` is the worked example. If it keeps no history at all, subclass
`HarnessObserver`, set `has_transcript = False`, and the entire observer is
`is_idle_screen`: the daemon stops looking for a file and reads the screen
instead, and that method becomes the signal that a turn ended rather than a hint
about a stuck agent.

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
| an `__init__` that never sets `self.observer` | `…/nova.py: harness 'nova' sets no observer. A harness must assign one in __init__ …` |
| `self.observer = NovaObserver` | `…/nova.py: harness 'nova' sets observer to the class NovaObserver, not an instance of it` |
| an observer subclassing neither base | `…/nova.py: harness 'nova' has a NovaObserver observer, which does not subclass theater.harness.HarnessObserver` |
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
    added = registry.install(Config(), local_dir=tmp_path)
    assert "nova" in added
    assert registry.HARNESSES["nova"].icon == "◈"
```

`install(config, local_dir=…)` is the seam: it rebuilds the registry from a
directory you choose, so nothing has to relocate `$THEATER_HOME`. It is
idempotent — calling it again rebuilds from scratch rather than accumulating —
and `install(Config())` restores the shipped set, which is how a test cleans up
after itself.

Instantiate the two classes directly, and test each for what it owns — the
launch plan on the harness, everything about reading on the observer:

```python
plan = NovaHarness().plan_launch(
    participant_id="abc123def456",
    prompt="hello",
    config_path=tmp_path / "mcp.json",
    approval="manual",
)
assert "--mcp-config" in plan.argv
assert "abc123def456" in plan.files[tmp_path / "mcp.json"]

obs = NovaObserver(root=tmp_path)          # never the real ~/.nova

events = obs.parse('{"role":"assistant","text":"hi","done":true}', 0)
assert [e.kind for e in events] == [EventKind.ASSISTANT]
assert events[0].turn_end

assert obs.parse("{not json", 0) == []     # torn line, no exception
assert obs.is_idle_screen("nova> ")
assert not obs.is_idle_screen("nova> what is")
```

`NovaHarness(root=tmp_path).observer` reaches the same object through the
harness, which is the path the daemon takes; either is fine in a test.

Take the constructor-injected root seriously. Every shipped observer has one
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
