---
name: theater-configure
description: Configure or personalize Theater interactively when the user wants to set up, review, or change their Theater settings — favourite CLI, régie theme and behaviour, which models and thinking levels agents may use, plugin enablement, safety limits, retention, and observability. Use for a guided question-by-question walkthrough of config.toml that discovers what it can from the machine and asks the rest in plain language, including first-time setup on a new machine.
---

# Theater Configure

Run a short, structured configuration interview and finish with a config.toml that contains
exactly what the user chose and nothing else. Theater is configured per machine, one file,
read at process start — `theater config path` prints where. Work in three movements: gather
every fact yourself, ask only for preferences, then write and validate.

## Session rules

- Use the host harness's native structured ask-user tool whenever one is available, giving
  concrete options for every question whose answers are enumerable; otherwise ask in a normal
  assistant response and stop until the user answers. Never invent an answer, and never write
  a key the user did not choose.
- Ask preference questions in plain, non-technical language — "What's your favourite CLI?",
  "What's your favourite theme?" — and map the answers to config keys yourself. The user
  never has to read or write TOML.
- Facts are yours to find, never the user's. Installed CLIs, available models, available
  plugins, and everything already in the file are discovered in step 1, not asked.
- Every question carries a recommended answer, so a reply can be a single word.
- A multi-select always offers a recommended combined option ("all of them", "none") so
  the default is one click; when a question has more answers than the ask tool's option
  limit, offer the recommended ones and let the rest arrive as free text.
- A setting that already has a value from the user's file is reported, not re-asked: "your
  theme is nord — keep it?" Only a requested change writes a key.
- The user may say "just the essentials" or "defaults everywhere" at any point. Then drop
  the remaining rounds, keep what is already answered, and go to the write-up.

## Step 1 — Read the machine

Run all of these before asking anything. They need no daemon running.

- `theater config path` — where the file lives.
- `theater config --json` — every resolved setting, tagged `config.toml` or `default`.
  These are the values to present; never re-ask one the file already answers.
- `theater harnesses --json` — which CLIs are installed, loadable, or broken.
- `theater models --discover <harness> --json` for vibe and opencode only — these two
  CLIs answer discovery. The discovered block is written whole in step 3, spellings
  untouched; it is what the user's CLIs are already configured to run.
- `theater plugins --json` — local plugin packages; note the MCP ones. If there are none,
  plugins are never mentioned again. Plugin packages live at `plugins/<name>/` under the
  home, and a plugin may ship its own skills at `plugins/<name>/skills/<skill-name>/SKILL.md` —
  this layout is constant on every machine, so those relative paths can be read directly.

### Models — the exact recipe per CLI

Available models are facts. Enumerate them, never guess, and never settle for the
default model — it is one entry, not the available set. pi, codex, and claude do not
answer discovery, so use these exact commands and record fields:

**pi** — run `pi --list-models`. Each row is
`provider  model  context  max-out  thinking  images`; take the first two columns only
and write each row as one entry spelled `provider/model` (for example
`mistral/zai-glm-5-2`) — that is the form pi's `--model` accepts. Every row is an
available model; do not filter. If the `pi` binary is missing, fall back to
`~/.pi/agent/models.json`: for every provider key in `providers`, every model under it
becomes one `provider/model` entry.

**codex** — union three sources:
1. `model = "…"` at the top of `~/.codex/config.toml`.
2. Every key of the `tui.model_availability_nux` table in that same file.
3. The models actually served, from session records: for each file matching
   `~/.codex/sessions/**/*.jsonl`, parse lines until one has `"type": "turn_context"`,
   take that record's `payload.model` once per file, then stop reading that file.
Skip `codex-auto-review` — an internal alias, not a picker entry.

**claude** — union two sources, then add aliases:
1. `model` in `~/.claude/settings.json`.
2. The models actually used: for each `~/.claude/projects/**/*.jsonl`, parse each line
   that has a `message` object, take `message.model`, skip the literal `<synthetic>`,
   keep distinct values only.
3. Add `sonnet`, `opus`, `haiku` — the tier aliases `claude --model` accepts, which
   track the newest version of each tier.

**Two traps, for every CLI.** Take model names only from the record fields named above,
never from message content — prompts and tool output routinely contain model-looking
strings that were never available. And if a harness produced no entries at all, leave
its key out and name it in the summary; never write a guess.

## Step 2 — Interview, in rounds

One round at a time; later questions may depend on earlier answers. Multi-select where marked.

### Round 1 — favourites

1. `theater.favourite` — "Which of these CLIs do you like best?" Options: the installed
   harnesses. Recommend the one this conversation runs in, or the first installed one.
   Absent is a valid answer: no favourite.
2. `regie.theme` — "What's your favourite theme for the dashboard?" Options:
   ansi-dark, ansi-light, atom-one-dark, atom-one-light, catppuccin-frappe,
   catppuccin-latte, catppuccin-macchiato, catppuccin-mocha, dracula, flexoki, gruvbox,
   monokai, nord, rose-pine, rose-pine-dawn, rose-pine-moon, solarized-dark,
   solarized-light, textual-dark, textual-light, tokyo-night. Offer a handful that suit
   the user's terminal plus free text. Unset means Textual's own default, and an unknown
   name is reported in the régie's status line rather than crashing — a miss is cosmetic.

### Round 2 — grants and plugins

Models and thinking levels are reproducible from the machine, so they are filled by
discovery, never asked. Tell the user during this round, before anything is written, and
again in the closing summary, plainly, that a listed model or thinking level is a spending
allowance — an agent that can spawn may use it without asking a human — so the user knows
what was granted before the file takes effect.

3. `[models]`, per harness — write the set the step 1 recipe produced for each
   installed CLI, whole and unfiltered, in the CLI's own spelling (exact and
   case-sensitive). Write a key only for a harness you actually filled.
4. `[reasoning]`, per harness that takes a thinking level — write every level the CLI
   advertises as of this release: codex — none, minimal, low, medium, high, xhigh, max,
   ultra; claude — low, medium, high; pi — the `--thinking <level>` line of `pi --help`
   lists exactly: off, minimal, low, medium, high, xhigh, max — run that one command and
   copy the list verbatim, never from memory. opencode and vibe take none — never write
   a key for them. Matching is exact.
5. `[mcp] enabled` — only if MCP plugin packages were found: "Do you want any of these
   extras switched on?" Options: the plugin names, multi-select. Some plugins need
   settings of their own before they will actually switch on — `theater plugins` shows
   each one's state and error. For each chosen plugin that does, in order:
   - Check whether the plugin ships a configure skill of its own —
     `plugins/<name>/skills/` under the home; reading the directory needs no daemon.
     If a configure skill is there, load it through your own skill-loading MCP tool or
     `theater skills` (both need a daemon) and follow it to fill the plugin's
     `[mcp.plugins.<name>]` table; that is the preferred path. With no daemon
     reachable, read that `SKILL.md` file directly and follow it — the direct read is
     instructions for you only, and nothing enters the skill registry through it.
   - If it ships none, ask plainly: "This extra needs its own custom settings before it
     will switch on — leave it off for now, or switch it on and you set it up yourself
     later?" Leave off: the name is not written into `enabled`. Switch on and DIY:
     write it, and say in the summary that it stays dormant until the user fills its
     `[mcp.plugins.<name>]` table by hand.
6. `[skills] disabled` — "Theater ships a few built-in helpers your agents can load —
   any of them switched off?" Options: the built-in skill names, multi-select,
   recommend none. Say in the question that this configure helper switches itself off
   at the end of the session by design, so "none" still ends with it disabled — the
   user who wants it kept can say so, and that counts as asking to keep it enabled.
   The names come from `theater skills` (needs a daemon) or your own
   skill-listing MCP tool; if neither is reachable, skip the question and write
   nothing. A name in the list is simply not offered to agents. Disabling is presence,
   not refusal — built-in skills stay validated, and an unknown name in the list is
   not an error.
7. `[harness] disabled` — never asked; every CLI is driven by default. Only when
   `theater harnesses` shows a broken plugin, recommend disabling it by its folder name
   and write the denylist entry if the user agrees. Disabling is absence, not refusal,
   and the name is matched against the plugin folder before import.

### Round 3 — limits

8. `rails.depth_cap` — "How many levels of delegation should be allowed?" A boss agent may
   hire a helper, who may hire a helper… Default 3.
9. `rails.budget` — "How many agents may share one boss before Theater says enough?"
   Default 20. Raise deliberately; these two are what stops a runaway agent.
10. `retention.jobs_days` — "How long should Theater remember what past jobs did?"
    Default two weeks. The automatic database tidy-up itself is never asked about — it
    stays on.

### Round 4 — the régie's feel

11. `regie.cost_window` — "The footer can total what agents have cost you — over today,
    this week, this month, or the whole year?" Options: day (default), week, month, year.
12. `regie.participant_detail` — "In the sidebar, should each agent show its folder or
    its description?" Options: cwd (default), description.
13. `regie.sidebar_width` — "How wide should the sidebar be, in characters?" Default 52;
    below 40 the tree rails no longer fit.

### Round 5 — observability

14. The observability gate — "Do you watch Theater in a telemetry dashboard (Grafana,
    Jaeger, an OpenTelemetry collector)? Turning it on exports Theater's traces, metrics,
    and logs to your collector, and needs the observability extra installed." Recommend
    no; with no, the export stays off and no key is written. If yes: turn the export on,
    ask for the collector address and whether it speaks grpc (default) or http, keep the
    service name "theater" unless the user cares, and ask the same way about the
    agent-signal toggles, the content opt-in, and the export and gauge intervals.

### Silently defaulted — never asked, never written

Everything else is a measured default that only a symptom or a curious user should
surface. Do not ask about it and do not write it; if the user names one, take the change
and write only that key. The surface, so you can answer a follow-up without re-reading
anything: the event log hidden on open; welcome animations on; built-in welcome
sentences; two folder segments per agent; tree refresh 1s; event-log poll 0.4s in
batches of 50; ledger page 30; sentence and tip typing pacing; every observer timer
(poll 0.25s, search 2s, relocate 5s, awaiting-input 1.5s, screen 1s, rescue 60s, sync
1s); the tidy-up hourly in batches of 5000, event feed kept 7 days, refused-send cap
10000, stale jobs 7 days; log files rolling at 10 MiB keeping 3 backups.

## Step 3 — Write the file

- Read the existing config.toml first, if there is one. Preserve every key, value, and
  comment the interview did not change; edit surgically, never rewrite the file wholesale.
  A fresh file contains only the chosen keys, each in its section.
- Write only what the user chose or what discovery filled. An unwritten key keeps tracking
  the code's default; a written one is pinned against future changes — so a setting kept at
  its default is left out of the file, not written down.
- Shapes: `favourite` and `theme` are strings; `[models]` and `[reasoning]` hold one list
  per harness; `[mcp] enabled`, `[skills] disabled`, and `[harness] disabled` are lists.
  Model and effort spellings are the CLI's own, exact and case-sensitive.
- Last write of the session: add `theater-configure` itself to `[skills] disabled`.
  The skill's work is done once the file validates, and a disabled configure skill
  cannot be loaded by accident later. This is the one deliberate exception to writing
  only what the user chose — it was announced in the interview's question 6, and the
  user overrules it by asking to keep the skill enabled. If the list already holds
  names, append — never drop one.
- Never write a secret into the file. An MCP plugin's secrets belong in its
  `[mcp.plugins.<name>]` table as `{ env = "NAME" }` or `{ file = "/path" }`; filling
  that table is the plugin's own configure skill's job when it ships one, otherwise
  the user's.
- Validate: `theater config` reports a malformed file exactly as the daemon would reject
  it, and `theater models` shows the resulting allowlists. Fix until both are clean.

## Step 4 — Close

Changes apply on `theater restart` — offer to run it, saying it restarts the daemon,
which reads the file once at start. The régie is not restarted by it: a régie that is
already open picks up config-dependent visuals (theme, sidebar width) only when it is
closed and relaunched. Summarize what was discovered, what was written where, what was
deliberately left absent (grants and pinned defaults), and what stays at its default.
Say plainly that theater-configure switched itself off in `[skills] disabled`, and that
undoing it is removing the name from that list and running `theater restart` again. Do
not create a report file.
