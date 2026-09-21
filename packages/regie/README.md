# Régie

Régie is Theater's independent Textual frontend and persistent tmux terminal
provider bridge. It depends on the exactly matching `theater` distribution and
uses only Theater's public frontend API.

Install matching candidates together, then run `regie`. It connects to a
compatible Theater daemon, starts the installed matching daemon only when one
is absent, ensures the bridge is ready, and opens the UI inside a reusable
window on the bridge's exact tmux server. `regie bridge start`, `status`, and
`stop` manage the bridge without terminating participant terminals.

Historical receipts with no matching daemon operation are preserved under
`$THEATER_HOME/regie/bridge/unmatched-receipts/`. They cannot settle jobs or
change participants and do not prevent the bridge from reconnecting.

Completed actions update the tree immediately, without waiting for its periodic
refresh. `var/logs/regie/*.log` records action admission, observed completion,
and tree-update latency with the daemon operation ID for correlation.
Action logs separate snapshot retrieval, unmanaged-pane discovery, projection,
and the wait until Textual's after-refresh callback; timings do not change the
reconciliation order or indicate that an unconfirmed operation has completed.
Routine success and background refreshes do not produce pop-up notifications;
action failures, uncertain outcomes, and startup failures remain visible.

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
existing `[regie]` table there manually. See the repository's
`docs/regie-config.example.toml` for every supported presentation setting.
