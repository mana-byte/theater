"""Human-presence detection for a tmux pane.

The spec (§10) says: never inject into a session a human is using. This
module checks whether a human is present at a pane before `send-keys`
delivers a prompt.

Detection combines two independent signals. Either one is sufficient to
block injection:

  1. **Copy mode** (`pane_in_mode`): the user is scrolling or selecting
     text. This is a tmux fact — no heuristic involved. A user in copy
     mode is definitely present and would lose their selection if we
     injected.

  2. **Input buffer** (`capture-pane` scrape): the last line of the pane
     has text beyond a bare prompt — someone has started typing but not
     yet pressed Enter. This is the primary signal for "a human is
     actively composing at this pane right now."

What is NOT a signal: `pane_active` + `session_attached`. Being attached
and looking at a pane does not mean the human is typing — the régie
stages agents so the user can watch them work, and blocking `send` to
any visible pane defeats the purpose. The spec says "when unsure, queue
rather than error"; a human who is merely watching is not using the
input line, so `send-keys` into the agent's pane is safe.

Tuned to accept false negatives and never false positives: a false
positive (injecting while a human is typing) corrupts the input line;
a false negative (queueing when nobody is there) just adds latency.
"""

from __future__ import annotations

from theater.tmux.client import run


async def human_present(pane_id: str) -> bool:
    """Is a human likely present at this pane?

    Returns True if there is evidence a human is actively using the pane's
    input. Returns False when unsure — the caller queues rather than
    errors, so a false negative is safe.
    """
    # Signal 1: copy mode or another tmux mode — definitely present.
    # This is a tmux fact, not a heuristic.
    try:
        in_mode = await run(
            "display-message", "-p", "-t", pane_id, "#{pane_in_mode}"
        )
    except Exception:
        # If we can't query the pane, assume no human — queue rather
        # than block forever.
        return False

    if in_mode and in_mode != "0":
        return True

    # Signal 2: scrape the input line for a non-empty buffer.
    # capture-pane -p gives the pane contents as plain text. The last
    # non-empty line is the current input line. If it has content beyond
    # a bare prompt, someone is typing.
    try:
        capture = await run("capture-pane", "-p", "-t", pane_id, check=False)
    except Exception:
        return False

    lines = [line for line in capture.splitlines() if line.strip()]
    if not lines:
        return False

    last_line = lines[-1].strip()
    # A bare prompt (e.g. "$", ">", "%", "#", or a short prompt like
    # "❯") is not a human typing. A line with content beyond a few
    # characters that does not end with a prompt symbol is suspicious —
    # someone has typed something and not yet submitted it.
    if len(last_line) > 3 and not last_line.endswith(("$", ">", "%", "#", "❯")):
        return True

    return False
