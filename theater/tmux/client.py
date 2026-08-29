"""Compatibility facade re-exporting the decomposed tmux modules.

Everything Theater knows about tmux used to live here. It has been split into
single-purpose modules — ``command`` (runner), ``facts`` (version/environment),
``panes`` (queries/mutations/staging), ``delivery`` (paste), ``options``
(session options/key bindings), and ``presence`` (human-presence) — and this
module re-exports the full surface so existing imports and monkeypatch seams
continue to work.

tmux is a hard dependency; if it is missing we fail loudly rather than
degrading, because there is no inbound delivery path without it.

Targets are always written ``session:``, never bare
-----------------------------------------------
tmux resolves a bare ``-t 0`` as *window index 0*, not as the session named
``0``, and unnamed sessions are named by number — so on a default setup
``new-window -t 0`` means "create at index 0" and fails with "index 0 in use".
The trailing colon makes it a session target and lets tmux choose the index.
This cost a real spawn failure; the argv is now asserted in
tests/test_tmux_client.py, because argv can be checked without a tmux server
and the behaviour cannot.

Verified against a real server by `tests/test_tmux_rig.py`, which runs a
private tmux (via `TMUX_TMPDIR`) with a program that logs every byte it
receives: `new_window` including its `-e` environment, session-scoped
`list_panes`, `kill_pane`, `display_message`, and the paste semantics
`deliver_text` depends on -- bracket markers around the text, Enter outside
them, one paste for a multi-line prompt, and no crosstalk between two panes
pasted at once. Reverting `deliver_text` to `send-keys -l` fails six of those
tests, which is the regression that suite exists to catch.
"""

from __future__ import annotations

from theater.tmux.buffers import set_buffer  # noqa: F401
from theater.tmux.command import (  # noqa: F401
    _FORMAT_SEP,
    _PANE_FORMAT,
    RUN_TIMEOUT,
    Pane,
    TmuxError,
    TmuxMissing,
    _require,
    run,
    run_sync,
)
from theater.tmux.delivery import deliver_text  # noqa: F401
from theater.tmux.facts import (  # noqa: F401
    _UNPROBED,
    _VERSION_CACHE,
    _parse_version_tuple,
    available,
    current_pane,
    current_session_sync,
    inside_tmux,
    reset_version_cache,
    tmux_at_least,
    tmux_version,
)
from theater.tmux.options import (  # noqa: F401
    bind_key_if_free,
    key_bound,
    set_option,
    set_window_option,
    show_option,
    show_window_option,
    unbind_key_if_owned,
    unset_option,
)
from theater.tmux.panes import (  # noqa: F401
    CreatedPane,
    TmuxInventory,
    TmuxServerIdentity,
    break_pane,
    display_message,
    ensure_session,
    join_pane,
    kill_pane,
    kill_pane_if_server_identity,
    kill_window,
    list_panes,
    move_window_to_index,
    new_window,
    new_window_named,
    new_window_with_identity,
    observe_inventory,
    pane_exists,
    pane_info,
    resize_pane,
    select_pane,
    sessions,
    split_window,
    swap_panes,
    window_for_pane,
)
