# v2 — Recall: what happened to this file, and who can tell me more

Supersedes `v2_ideas.md` §1 and §5. Those sketched a retrieval system over
session segments. This is narrower and, I think, more useful: a per-file
timeline of what Theater watched happen, with enough on each point to resume
the agent that made it.

The question an orchestrator actually asks is never "find me a relevant
session". It is *"I am about to touch this file — what happened here recently,
and is there somebody who already knows?"* That question is anchored on a path,
answered by recency, and ends in either reading a result or resuming a session.
Every design decision below falls out of taking that sentence literally.

Three pieces, in this order:

1. **Path capture** — record which files each job read and wrote, and the git
   blob hash before and after.
2. **`spawn_session(resume=…)`** — attach a new job to an existing harness
   session instead of starting cold.
3. **`recall(paths, depth)`** — the timeline, over MCP.

Only the first has a clock on it. Jobs that run before it exists are
permanently unindexable, so it ships first even though it is the least
interesting.

## Ground truth at 9ef668c

```
~/.theater/theater.db     24 MB, journal_mode=wal, busy_timeout unset
                          204 participants, 325 jobs, 26,725 bus events
                          span 2026-08-11 → 2026-08-15
jobs                      159 send (159/159 have cwd + session_id + prompt)
                          166 spawn (166/166 cwd + prompt, 133/166 session_id)
                          316 done, 9 crashed
agent.tool_call payload   {"text": "", "tool": "edit", "index": 90}
                          6,772 calls recorded, zero with non-empty text,
                          all four harnesses
theater/harness/base.py   Event(kind, text, tool_name, ts, turn_end, turn_id,
                :158      raw_index) — no field carries tool arguments
                :253      Harness.plan_launch ABC
theater/harness/__init__  supports_model() reads inspect.signature(plan_launch)
           :219,230,241   check_model() raises BadRequest, callable before
                          anything is created; plan_launch() funnel forwards
                          **extra only when non-None — the plugin compat seam
builtin/plugins/*.py      plan_launch at claude:147 codex:216 opencode:268
                          vibe:194
theater/daemon/spawner    SpawnRequest at :37, model field at :50-53
                :67       check_model runs before participant and worktree
theater/daemon/store.py   _set_pragmas at :46 — WAL and foreign_keys, no
                          busy_timeout
theater/daemon/migrations Alembic; a schema change without a revision fails
                          tests/test_migrations.py
transcripts               147 file paths recorded, 144 still on disk, plus 54
                          opencode://ses_… URIs (opencode keeps one shared
                          SQLite, not a file per session)
cwds                      30 distinct; 162 participants in
                          ~/Desktop/tui_cli_orchestration, 4 in this repo
pyproject.toml            4 runtime deps: mcp, textual, sqlalchemy, alembic.
                          No git library, no tmux library
theater/daemon/worktree   shells out to `git` at 9 call sites
theater/tmux/client.py    325 lines, 9 tmux subcommands, all subprocess
```

Four consequences drive everything below.

**Theater already sees every tool call and throws the arguments away.** The
payload is `{"text": "", "tool": "edit"}` — the *name* of the tool, never its
target. This is not an oversight in the store; it is upstream, in `Event`
(`base.py:158`), which has no field to put a path in. That single gap is why
recall cannot be built today, and closing it is nine tenths of piece 1.

**`jobs` is already the segment table.** `v2_ideas.md` proposed
`segment(id, session_id, ordinal, origin, task_text, result_text, …)`. Every
column of it exists on `jobs` under another name: prompt is `task_text`, result
is `result_text`, state is `outcome`, `created_at`/`finished_at` are
`record_start`/`record_end`. And `origin` would be `'job'` for 100% of rows,
because a job boundary is the only boundary Theater has. So no new table.

**Nothing prunes the bus.** 26,725 rows and growing, by design — `schema.py`
says so, and the AUTOINCREMENT comment anticipates pruning that has not
happened. Recall must not add a second unbounded table. `touch` is bounded by
files-touched-per-job, which is small.

**The dogfood corpus is not in this repo.** Four participants have ever run
here against 162 in `tui_cli_orchestration`. Test recall there.

## Decisions

Each is recorded with the alternative it beat, because the alternative is
usually the obvious choice and the reason it loses is not.

### Capture paths at parse time, in the plugins

The alternative is a filesystem watcher, or diffing the worktree at job end.
Both are harness-agnostic and neither can attribute. A watcher sees a write and
does not know which of three concurrent agents did it; an end-of-job diff
cannot separate a file the job wrote from a file its sibling wrote in the same
window. The plugin is the only place where "this tool call, from this
participant, targeted this path" is a fact rather than an inference.

The cost is that each plugin must know its own argument dialect — Claude's
`tool_use.input.file_path`, Codex's `function_call.arguments` JSON, and so on.
That is exactly the work a harness plugin exists to do, and it is where every
other dialect difference already lives.

Mechanically this is a new field on `Event`:

```python
#: Repo-relative paths this tool call touched, and how. Empty for tool calls
#: that touch no file. Filled by the plugin because only the plugin knows its
#: harness's argument spelling.
paths: tuple[tuple[str, str], ...] = ()   # (path, "read" | "write")
```

Defaulted, so a third-party adapter that never sets it keeps working and simply
contributes nothing to recall. Same compat posture as `model`.

### Per-path blob hashes, not a HEAD SHA

`v2_ideas.md` proposed `head_sha_start` / `head_sha_end` on the session. HEAD is
wrong three separate ways:

- it is blind to uncommitted edits, and most agent work is uncommitted when the
  job ends;
- it moves when the job commits, so a job's own commit reads as drift against
  itself;
- it is meaningless across branches, and there are 26 worktree spawns in the
  database already, each on its own `theater/<id>` branch.

A git blob hash is the content hash of one file, is defined for uncommitted and
untracked files, and is comparable across branches because it depends on
nothing but the bytes. It is also trivial to compute — see the SDK decision
below, which is why no subprocess appears in piece 1.

### Both before and after, because git cannot attribute

The obvious economy is to store only `sha_after` and diff against the next
point. It fails for the reason that motivates this whole document: **every
agent commits as the same human.** `git log --follow` on a file tells you
`manaiki.laut` changed it fourteen times. It cannot tell you that change nine
was a codex child working on the resume flag.

`sha_before` is what attributes a change to *this job*. It also makes the
hashes **chain**: job A's `sha_after` for a path should equal job B's
`sha_before` for the same path if B is the next job to touch it. When they do
not match, something changed that file that Theater never observed — a human in
an editor, a `git checkout`, an unmanaged pane. That gap is the one thing this
gives you that `git log` does not, and it only exists because we stored both
ends.

### Hash at job end, not at job start

Taking `sha_after` when the job finishes means a job's own writes are already
folded in, so it never reports itself as drift. The alternative — hash at the
next job's start — leaves a window in which the answer is wrong, and needs a
rule for the last job on a path.

### No git SDK, and no tmux SDK

Both were considered and both lose, for the same underlying reason: the part
that looks like it wants a library is trivial, and the part that is not trivial
is exactly the part a library would hide.

**Git.** A blob hash is `sha1(b"blob %d\0" % len(data) + data)`. Measured
against `git hash-object` on 43 real files in this repo, 43 of 43 matched:

```
pure python (hashlib) :   1.1 ms
git, one batched call :  28.7 ms
git, one call per file: 985   ms   ← the naive shape
```

So piece 1 needs no library *and no subprocess*. The candidates, for the
record: **GitPython** wraps the git CLI, so it still forks and still needs the
binary — nothing gained, and sources disagree on whether it is maintained or in
maintenance mode. **pygit2** 1.20.0 is current, typed and healthy, but it is a
5.7 MB binary wheel with one maintainer, a hard pygit2↔libgit2 minor-version
coupling, and GPLv2-with-linking-exception against four permissive deps — all
to replace two subprocess calls. **dulwich** is pure Python and its selling
point is dropping the git binary, a dividend Theater cannot collect because
`worktree.py` hard-requires `git` at nine call sites anyway.

What *does* need real git is drift, and only there: `git status --porcelain`
and `git diff --name-only` depend on gitignore rules and index state, which is
precisely where reimplementing would be wrong. Two subprocesses per query,
matching the existing pattern.

**Caveat, and it must not be lost.** The 43/43 match proves filters are
*inactive here*, not that raw SHA-1 always equals `git hash-object`. That
command applies `.gitattributes` filters by default — `--no-filters` is what
turns them off — so in a repo with CRLF conversion or an LFS clean filter the
two answers diverge. This is acceptable only because of a property worth
stating outright: **we only ever compare our hashes to our own hashes.** The
chain, the drift check and gap detection are all internal. Raw bytes is
arguably the better answer to "did this file change on disk" anyway. But
without this paragraph somebody will eventually run `git hash-object`, compare
it to a `touch.sha_after`, and conclude the index is corrupt.

**Tmux.** `libtmux` 0.62.0 is maintained and supports control mode, but its own
documentation says the API will keep changing through 2026 and asks callers to
pin. Against that, `theater/tmux/client.py` is 325 lines using nine
subcommands, and its docstring records two behaviours a wrapper would erase:
targets must be written `session:` with the trailing colon, asserted in
`tests/test_tmux_client.py` **by checking argv** — a test that cannot be
written if a library builds the argv; and `deliver_text` must use bracketed
paste rather than `send-keys -l`, where reverting fails six tests in
`tests/test_tmux_rig.py` against a real server. libtmux's `send_keys` is the
shape those six tests exist to reject. The tmux layer is not a generic wrapper
that happens to be hand-rolled; it is a small set of empirically-derived
behaviours with a regression suite around them.

### One timeline, not candidates plus history

An earlier sketch returned two shapes: `candidates`, a ranked list of jobs, and
`history`, a timeline keyed by path. The split was defensible — history is a
property of the path, so nesting it inside each candidate would duplicate it —
but it answers a question nobody asks. Ranking earns its place only if a
relevant job might have touched none of the paths you asked about. Under
path-first retrieval no such job exists, so the ranked view carries no
information the timeline lacks.

Collapsing them buys something real: **drift stops being a field.** In a
timeline ordered by change, position *is* drift — the newest point is current,
everything below it is stale by exactly the number of points above it. The only
thing position cannot express is an uncommitted edit sitting on top of the
newest point, and that is one flag per path (`dirty`), not one per candidate.

The cost is that a job touching three of your paths appears three times. That
is acceptable and arguably desirable: a job showing up under every path you
asked about *is* the ranking signal, legible without a score. If duplication
becomes a problem at `depth: 10` across many paths, the fix is a top-level
`jobs: {handle: {…}}` intern table with points holding only `handle` —
normalised, at the cost of making the model do a join.

### Reads are a count, not timeline points

A read has `sha_before == sha_after`. Rendered as points they outnumber the
writes — nine reads to four writes on `harness/__init__.py` in the sample — and
bury the changes. One integer per path, `reads`, keeps the signal ("this file
is load-bearing, lots of agents consult it") without the noise.

### Ranking is a sort, never a score

Same repo, then path overlap, then recency, then outcome. No learned ranker, no
similarity threshold. Path-first fails **closed**: no overlap, no result.
Vector search fails **open** — it always returns its top *k*, however
irrelevant. That asymmetry is tolerable in a search box a human reads and
dangerous here, where the output can be pasted into a child's prompt.

### SQLite stays; no vector store

The corpus is 325 short strings in a 24 MB file with, by construction, exactly
one writer: the daemon owns the store and everything else reaches it over
JSON-RPC. WAL is already on. This is the tier SQLite is *for*, and it is what
Letta uses for the same job.

sqlite-vec deserves its own note because it looks like a migration and is not:
it is a loadable extension over ordinary tables. Adopting it later costs the
same as adopting it now, which means there is no argument from lock-in for
doing it now — and one good argument against, below.

### No embeddings

Not "not yet, for effort reasons" — the constraint is structural. Embeddings
need an embedder, an embedder needs an API key, and a daemon-owned model config
or API key is an explicit non-goal in `v2_ideas.md` §5. Local models trade that
for a model download and a warm-up cost on a daemon that currently starts in
milliseconds.

If embeddings ever arrive they should be a **query expander**, not a retriever:
free text → candidate paths → the path-first machinery below. That keeps the
fail-closed property. A retriever that returns segments directly does not.

### No generated "why" prose

`v2_ideas.md` §1 already got this right and it is worth restating: the `why`
string renders facts, it does not invent relevance. Here it does not exist at
all, because the task text *is* the why and the result text is the what. Both
were written by the agent that did the work. Anything better would require an
LLM call at query time, which is the same API key problem in a new hat.

### `resume` mirrors `model` exactly

Not a new mechanism. `model` already solved this shape: an optional capability
that some adapters have, validated up front, forwarded only when asked for.
Copy it line for line — `SpawnRequest.resume`, a `check_resume()` beside
`check_model()`, `extra["resume"]` in the funnel. Anything invented here would
be a second way to do one thing.

## The shape

```text
recall(paths: string[], depth?: int = 5)
  -> { [path]: PathTimeline }

recall_read(segment_id: string)
  -> Brief
```

Two tools. `recall_read` stays separate on a principle the other does not
share: it takes a segment id rather than paths, and it is the only one
permitted to spend a `git log` — to reconstruct what happened inside a gap.

Input:

```json
{
  "paths": ["theater/harness/__init__.py", "theater/daemon/spawner.py"],
  "depth": 4
}
```

Output:

```json
{
  "theater/harness/__init__.py": {
    "current": "7b3f88a",
    "dirty": true,
    "reads": 9,
    "timeline": [
      {
        "sha": "e91c4d2 → 7b3f88a",
        "when": "2026-08-14T16:22:07Z",
        "handle": "codex-a41f",
        "harness": "codex",
        "session_id": "01JC8X2K9M4NPQR7VWZ3TYBH5D",
        "resume": true,
        "cwd": "/Users/manaiki.laut/Desktop/theater",
        "branch": "main",
        "outcome": "done",
        "task": "Add a `model` parameter to spawn_session, validated up front against the config allowlist.",
        "result": "check_model() raising BadRequest, wired in before participant creation. plan_launch forwards **extra only when non-None."
      },
      {
        "gap": true,
        "sha": "2a0ff31 → e91c4d2",
        "note": "no job claims this transition"
      },
      {
        "sha": "cc7e105 → 2a0ff31",
        "when": "2026-08-13T09:41:55Z",
        "handle": "vibe-3c07",
        "harness": "vibe",
        "session_id": "af7ab6c4-qz00lhjr",
        "resume": true,
        "branch": "theater/vibe-3c07",
        "outcome": "crashed",
        "task": "Make supports_model() introspect plan_launch instead of hardcoding.",
        "result": null
      }
    ]
  }
}
```

Three things in that output are load-bearing.

**The crashed job still appears.** It wrote to the file and its `sha_after` is
in the chain; somebody needs to know an incomplete edit landed there. Outcome
sorts a job down, it never filters it out.

**The gap.** `vibe-3c07` left the file at `2a0ff31`; `codex-a41f` found it at
`e91c4d2`. Nothing in `touch` explains the transition. This is precisely when a
caller should spend a `recall_read`.

**`resume` is a capability, not a guess.** It is `false` with a note for a
harness that cannot be resumed programmatically, so the caller learns it here
rather than at spawn.

`task` and `result` clip to ~300 characters. Full text lives behind
`recall_read`.

## Piece 1 — path capture

```sql
touch(job_handle, path, mode, sha_before, sha_after)
```

`mode` is `read` or `write`. `path` is repo-relative — absolute paths leak home
directories into an index that gets pasted into prompts. One Alembic revision
under `theater/daemon/migrations`, or `tests/test_migrations.py` fails.

Order of work:

1. Add `paths` to `Event` (`base.py:158`), defaulted to `()`.
2. Fill it in each plugin's parse, four sites, one dialect each.
3. Hash each path with `hashlib` the moment it is first seen, and hash them all
   again at job end. Missing file → null hash, which is how a deletion is
   represented.

   ```python
   def blob_sha(path: Path) -> str | None:
       """git's own blob hash, computed without git.

       `git hash-object` is one fork per file and 900x slower across a job's
       worth of paths. The format is not a secret: SHA-1 over a header and the
       raw bytes. Raw, deliberately — see the SDK decision on `.gitattributes`
       filters.
       """
       try:
           data = path.read_bytes()
       except OSError:
           return None  # gone, or never existed: a deletion
       h = hashlib.sha1()
       h.update(b"blob %d\0" % len(data))
       h.update(data)
       return h.hexdigest()
   ```
4. Write `touch` rows on job completion, in the same transaction as the job
   result.

Non-git directories simply produce null hashes and still record the path. The
timeline degrades to "who touched this" without the chain, which is worth
having.

## Piece 2 — `spawn_session(resume=…)`

Verified against source at `~/Desktop/coding_clis/`:

| harness  | form                       | prompt survives resume? |
|----------|----------------------------|-------------------------|
| codex    | `resume <id>` subcommand   | yes (`main.rs:3616`)    |
| vibe     | `--resume <id>`            | yes (`cli.py:259`)      |
| claude   | `--resume <id> --fork-session` | yes                 |
| opencode | `-s` / `--session <id>`    | **no — dropped**        |

Per-harness traps:

- **codex** — `-c` is `global = true`, so `resume <id>` goes after flags and
  before the prompt. Requires `-c tui.resume_cwd=current` or it stalls on an
  interactive cwd prompt (`session_resume.rs:140-150`). It holds a cross-process
  `flock`; a second attach fails with "already has an active writer"
  (`writer_lock.rs:64-70`) and that error must be surfaced, not swallowed.
- **vibe** — `--resume` is `nargs="?"`. Never emit it bare or it eats the
  prompt as the session id.
- **opencode** — `-s` routes to the session view (`app.tsx:492`) and `--prompt`
  only fires on the home screen (`home.tsx:64`), so the prompt is silently
  discarded. Silently is the problem: the job would look launched and never
  receive its task.
- **claude** — Claude Code 2.0.73 accepts a fresh `--session-id` with
  `--resume <id> --fork-session`. It resolves resume/fork sessions in the
  transcript's current project cwd, so Theater requires a materialized native
  JSONL and launches from its latest recorded cwd rather than the predecessor
  row's original cwd.

opencode needs a capability that a function signature cannot express — it
*accepts* a resume flag and cannot carry a prompt through it. So
`supports_model`'s introspection trick does not generalise, and it takes a
class attribute:

```python
#: Whether a resumed session can still be handed a prompt on the command line.
#: False for opencode: `-s` routes to the session view and `--prompt` is only
#: read on the home screen, so the task would be dropped without an error.
resume_takes_prompt: bool = True
```

Checked in the same up-front gate as `check_model`, at `spawner.py:67`, before
the participant exists.

Two combinations are refused there as well:

- `resume` with `worktree=True` — a session resumed into a fresh worktree has a
  transcript describing files that are not the files it is now looking at;
- resuming a participant that is still live — that is a `send`, and `send`
  already exists.

Codex and Vibe continue the same native session id on resume (codex appends to
the same rollout, `recorder.rs:895`; vibe to the same `messages.jsonl`,
`_runtime.py:265`). Two participants can therefore claim one transcript. The
mitigation is to seek to the end at attach: seed `after=` to the current
transcript end so the resumed observer replays nothing. Claude instead forks
to a fresh native id and transcript.

Session-id coverage on spawns is opencode 98%, vibe 92%, codex 83%, **claude
47%**. Claude can resume but half its spawns have no recorded id to resume
from. Cause unknown; the guess is short sessions dying before the observer
recovers the id. Worth a look before advertising resume for claude.

## Piece 3 — `recall`

A join over `jobs` + `participants` + `touch`, ordered by `finished_at`
descending per path, capped at `depth`.

Drift is the only part that needs live git, and the naive shape is one
subprocess per candidate path — measured at 985 ms across 43 files, which is a
query budget spent entirely on fork. Do it **per repo, not per candidate**: one
`git status --porcelain` for the dirty set (30 ms), one
`git diff --name-only <oldest_head>..HEAD` for the committed set, union them,
and intersect in SQL. Two subprocesses regardless of result count.

These two stay as subprocesses on purpose — they answer questions about
gitignore rules and index state that we should not be reimplementing. The
hashing, which is just bytes, does not.

Gap detection is pure SQL: order a path's rows by time and compare each
`sha_before` against the previous row's `sha_after`. No git needed until
somebody calls `recall_read` to ask what happened inside one.

## Deliberately excluded

- **`query` / FTS5 in v1.** Filtering a timeline breaks the hash chain: every
  omitted point looks like a gap, so the one feature that distinguishes this
  from `git log` starts lying. Addable later as a non-filtering `matches: true`
  marker, which does not change the shape.
- **Cross-path ranking.** See the timeline decision above.
- **An `action` field** (`ask` / `brief` / `read-yourself`, from `v2_ideas.md`
  §1). The facts that would produce it — `resume`, `dirty`, position, `outcome`
  — are all in the payload. Collapsing them into one word discards the reason.
- **SCIP, AST-level diffing, difftastic / diffsitter / SemanticDiff.** The
  ceiling, not the floor: they would let a point say *what* changed
  semantically rather than *that* it changed. None is installed here. A later
  optional refinement is to shell out to `difft` when it is on PATH.
- **CLI commands for recall.** MCP only. The consumer is an agent.
- **Automatic prompt injection at spawn.** Unchanged from `v2_ideas.md` §5: the
  parent composes briefings explicitly.

## Safety rules, carried forward unchanged

From `v2_ideas.md` §1, all still binding:

- Git root is a hard privacy wall. Cross-root recall is configuration, not a
  query flag.
- Index references and derived facts, never payloads: no file contents, no
  diffs, no command output.
- Redact credential-shaped text before writing index rows.
- Brief-derived text must not feed back into the index as future evidence.
- `theater forget` must purge `touch` rows along with everything else.

## Loose ends

- `busy_timeout` is unset (`store.py:46`), which means a contended write fails
  immediately rather than waiting. Worth fixing regardless of this work; recall
  adds readers, which makes it likelier to bite.
- Claude records a session id on 47% of spawns. Cause unknown.
- 3 of 147 recorded transcript paths are gone from disk. `recall_read` needs a
  graceful answer for a timeline point whose transcript no longer exists.
- opencode's 54 `opencode://ses_…` URIs point into one shared SQLite database
  rather than per-session files. `recall_read` needs a reader for it.
