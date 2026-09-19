# Régie

Régie is Theater's independent Textual frontend and persistent tmux terminal
provider bridge. It depends on the exactly matching `theater` distribution and
uses only Theater's public frontend API.

Install matching candidates together, then run `regie`. It connects to a
compatible Theater daemon, starts the installed matching daemon only when one
is absent, ensures the bridge is ready, and opens the UI inside a reusable
window on the bridge's exact tmux server. `regie bridge start`, `status`, and
`stop` manage the bridge without terminating participant terminals.

Régie configuration lives only at `$THEATER_HOME/regie/config.toml`; move an
existing `[regie]` table there manually. See the repository's
`docs/regie-config.example.toml` for every supported presentation setting.
