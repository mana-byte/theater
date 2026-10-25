# Régie

Régie is Theater's independent Textual frontend and persistent tmux terminal
provider bridge. It depends on the exactly matching `theater` distribution and
uses only Theater's public frontend API.

Install matching candidates together, then run `regie`. It connects to a
compatible Theater daemon, starts the installed matching daemon only when one
is absent, ensures the bridge is ready, and opens the UI inside a reusable
window on the bridge's exact tmux server. `regie bridge start`, `status`, and
`stop` manage the bridge without terminating participant terminals.

The first UI frame appears after reversible tmux presentation setup, before
initial data reads. Catalog, state, usage, and event reads then run concurrently;
the tree waits for catalog-backed icons and pane discovery, not the usage footer.
Its reveal animation is unchanged. The spawn palette waits for catalog loading,
and quitting cancels pending startup reads before restoring presentation.

Régie keeps its own tree, apart from Theater's: `$THEATER_HOME/regie/config.toml` is
what you edit, and everything Régie writes lives under `regie/var/`: `state/` (tree
layout, bridge state), `run/` (the bridge's pid, status and locks) and `logs/`
(`bridge.log`, `ui/`, `bridge-stderr/`). An older, flat `regie/` moves itself there
on the next start, stopping a bridge still running from it first.

## Reading the tree

The tree is the whole interface; every signal is a state of something it already draws.

- **Status glyph.** A spinner is an agent computing; `!` needs you (awaiting input); `✗` is
  dead. A slow square quadrant `▘▝▗▖` is an agent blocked on a peer, in the colour of that
  wait; a ring filling `○◔◑◕●` is a spawn starting, with the daemon's phase on the row's
  third line; `✕` is a kill in flight.
- **Bar.** A `▌` in the gutter marks a row that needs you; the newest three breathe.
- **Sweep.** A block runs across a row, in a colour that says what happened: cyan when the
  spawn row opens and green when the agent is up (or a turn finishes, or a send is delivered),
  red when a kill begins or an agent dies, yellow when an agent needs you or an operation is
  blocked or in doubt. A row the daemon has already dropped stays until its sweep ends, then
  leaves with the reverse reveal if it was killed, roots included.
- **Sends and waits.** A send travels the rails as a comet in its sender's colour. Waits show
  `◂` on the agent being waited on (`◂2` for two); only a new wait and the selected row's waits
  draw their route.
- **Marks.** After the name: `⋯` queued, `✓` delivered (fades), `⊘` blocked (not now: human
  present, busy), `✗` failed, `?` unconfirmed. Failures say why on the third line and stop
  asking once the cursor has rested on the row.
- **`]` / `[`** jump to the next or previous row that needs you, wrapping round. A folded
  section shows the worst state it hides.

`reduce_motion = true` in `config.toml` keeps every signal and drops the motion: no sweeps,
breathing, comets or reveal and retire animations, and waits draw their route without a
travelling head.

## Ambience plugins

The animations under the tree are package-manifest plugins, like Theater's harnesses. Put one
in `$THEATER_HOME/regie/plugins/<name>/` and select it with `tree_ambience = "<name>"`:

```python
# plugins/glow/manifest.py
from regie.ambience.api import AmbienceManifest

from .scene import Glow  # sibling modules import each other relatively

MANIFEST = AmbienceManifest(scene=Glow)  # Glow.name must be "glow", the folder's name
```

A scene subclasses `regie.ambience.api.Scene` and returns `Cell`s from `frame`; import only
from `regie.ambience.api`. A local plugin replaces a shipped one of the same name. A broken
plugin is logged and skipped, and refused only if the configuration names it.

Historical receipts with no matching daemon operation are preserved under
`$THEATER_HOME/regie/var/state/bridge/unmatched-receipts/`. They cannot settle jobs or
change participants and do not prevent the bridge from reconnecting.

Completed actions update the tree immediately, without waiting for its periodic
refresh. `$THEATER_HOME/regie/var/logs/ui/*.log` records action admission, observed completion,
and tree-update latency with the daemon operation ID for correlation.
Action logs separate snapshot retrieval, unmanaged-pane discovery, projection,
and the wait until Textual's after-refresh callback; timings do not change the
reconciliation order or indicate that an unconfirmed operation has completed.
Routine success and background refreshes do not produce pop-up notifications;
action failures, uncertain outcomes, and startup failures remain visible.

A spawn is traced from the Enter on its directory row to the staged terminal, one line per step,
all carrying the daemon's operation id: `spawn.enter_to_submit` (Régie's own delay), `spawn.phase`
(each daemon phase, with `daemon_at`, how long into the operation the daemon wrote it, and `late`,
how long after that Régie drew it; a warning past 250ms), `spawn.participant` (when the new row
appears, gets a terminal route, changes status), then `spawn.visible` and `spawn.staged` (a
warning past one second). The `+Nms` on each is counted from the Enter. Picking a harness is
timed separately by `interaction.spawn_menu.row` and `.prompt`. `late` large means the update
reached Régie slowly; `daemon_at` large means the daemon was slow; both small with a slow screen
means the delay was in the terminal.

`terminal.write blocked` and `terminal.flush blocked` (a write to the terminal that took over
100ms) and `terminal.queue N frames waiting` (frames piling up behind a slow terminal) name a
screen that is late while Régie itself is on time: every timer and animation inside Régie keeps
running, but the human sees the result later.

Local `startup.*` logs separate presentation, catalog, snapshot, unmanaged-pane,
projection, usage, and event-read time. `first_frame`, `participants_ready`, and
`ready` mark after-refresh milestones measured from app construction, excluding
Python imports and CLI preflight. They are not shell-to-interactive measurements
or reveal-animation completion times; `ready` means initial reads have settled,
including recoverable failures.

The Spawn submenu shows one `Spawn <harness>` entry per harness. Selecting it
opens a directory picker with filesystem completion; nothing launches until the
directory is confirmed. Accept the prefilled path to use Régie's current directory.

Killing the staged participant first restores its terminal to a background
window, releasing local input focus. The daemon still requires fresh human
absence and the provider still verifies the exact terminal identity; another
viewer or uncertain presence can refuse termination. Agent self-kill stays forbidden.

When a harness exits and tmux removes its pane, the bridge proves that the
recorded pane is absent on the original server so Theater can retire the
participant. Failed inspections and replacement servers remain uncertain.
Interrupts use the harness's declared keys; an idle interrupt sends no input.

The bridge enables tmux focus reporting and owns non-destructive focus hooks.
Focus and pane changes promptly invalidate Theater's cached human presence.
Blur releases protection only after a verified focus transition for the same
client lifetime; uncertain evidence stays protected. Already attached clients
may need reattachment when focus reporting was previously disabled. Stopping
the bridge removes only its hooks and leaves focus reporting enabled.

Régie configuration lives only at `$THEATER_HOME/regie/config.toml`; move an
existing `[regie]` table there manually. See `config.example.toml` beside this README
for every supported presentation setting and every shipped ambience.
