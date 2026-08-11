"""Human-presence detection for a tmux pane.

The spec (§10) says: never inject into a session a human is using. This
module checks whether a human is present at a pane before `send-keys`
delivers a prompt.

Detection combines:
  - `pane_active` + `session_attached > 0`: an attached human is looking
    at this pane.
  - `pane_in_mode`: copy mode — definitely present.
  - `capture-pane` scrape of the input line: a non-empty buffer means
    someone is typing.

Tuned to accept false negatives and never false positives: when unsure,
queue rather than error. A false positive (injecting while a human is
typing) corrupts the input line; a false negative (queuing when nobody
is there) just adds latency.
"""

from __future__ import annotations

from theater.tmux.client import run


async def human_present(pane_id: str) -> bool:
    """Is a human likely present at this pane?

    Returns True if any signal indicates a human is actively using the
    pane. Returns False when unsure — the caller queues rather than
    errors, so a false negative is safe.

    Signals (any one is sufficient):
      1. `pane_in_mode` is non-empty (copy mode, etc.) — definitely present.
      2. `pane_active` is 1 AND `session_attached` > 0 — an attached
         human is looking at this pane.
      3. The last line of `capture-pane` has a non-empty input buffer
         (text after the prompt, not yet submitted) — someone is typing.

    Signal 3 is the most reliable but also the most fragile: it depends
    on the prompt format and the pane's scroll position. Signals 1 and 2
    are tmux facts, not heuristics.
    """
    # Check tmux's own pane state — these are facts, not heuristics.
    fmt = "#{pane_in_mode}\t#{pane_active}\t#{session_attached}"
    try:
        out = await run("display-message", "-p", "-t", pane_id, fmt)
    except Exception:
        # If we can't query the pane, assume no human — queue rather
        # than block forever.
        return False

    parts = out.split("\t")
    if len(parts) != 3:
        return False

    in_mode, pane_active, session_attached = parts

    # Signal 1: copy mode or another mode — definitely present.
    if in_mode and in_mode != "0":
        return True

    # Signal 2: the pane is active in an attached session.
    if pane_active == "1" and int(session_attached or "0") > 0:
        return True

    # Signal 3: scrape the input line for a non-empty buffer.
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
    # A bare prompt (e.g. "$", ">", "%") is not a human typing. A line
    # with content beyond a single prompt character is suspicious. This
    # is deliberately conservative: we only flag presence when there is
    # visible input, not just a cursor on a prompt line.
    if len(last_line) > 3 and not last_line.endswith(("$", ">", "%", "#")):
        return True

    return False
