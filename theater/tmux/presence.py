"""Human-presence detection for a tmux pane.

The spec (§10) says: never inject into a session a human is using. This
module checks whether a human is present at a pane before `send-keys`
delivers a prompt.

Only one signal is used:

  **Copy mode** (`pane_in_mode`): the user is scrolling or selecting
  text. This is a tmux fact — no heuristic involved. A user in copy
  mode is definitely present and would lose their selection if we
  injected.

The `capture-pane` input-buffer scrape was removed because it cannot
distinguish agent rendered output (the last line of an agent's pane is
almost always non-empty text) from a human's unsubmitted input. The
false-positive rate was unacceptable: it blocked legitimate sends to
agents that were simply displaying output. The spec says "tuned to
accept false negatives and never false positives" — the scrape violated
that.

Future improvement: `pane_in_mode` combined with a harness-specific
prompt matcher (e.g. "does the last line end with the exact prompt
string this harness uses, and is there text before it?") would be
reliable. But that requires knowing each harness's prompt format,
which is not stable across versions. Until then, copy mode is the
only signal that is both reliable and safe.
"""

from __future__ import annotations

from theater.tmux.client import run


async def human_present(pane_id: str) -> bool:
    """Is a human likely present at this pane?

    Returns True if there is evidence a human is actively using the pane.
    Returns False when unsure — the caller queues rather than errors,
    so a false negative is safe.
    """
    try:
        in_mode = await run(
            "display-message", "-p", "-t", pane_id, "#{pane_in_mode}"
        )
    except Exception:
        # If we can't query the pane, assume no human — queue rather
        # than block forever.
        return False

    return bool(in_mode and in_mode != "0")
