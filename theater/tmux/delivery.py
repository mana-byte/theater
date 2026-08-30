"""Insert text into a tmux pane as a paste, not as keystrokes.

Not ``send-keys``, which is what this used to be and which is wrong for a
TUI. ``send-keys -l`` is literal only in the sense that tmux does not read
the text as *key names*; the characters still arrive one by one, exactly
as if a human had typed them, and every keybinding on the far side fires.
That is not a theoretical problem. OpenCode binds ``!`` to shell mode, so
sending

    Hey! Quick fun debate ... over ~10 short dialogue lines

swallowed the ``!``, flipped the composer into shell mode, and the following
Enter ran the rest of the sentence through zsh -- which answered
"not enough directory stack entries", because ``~10`` is a directory stack
reference. An earlier prompt died on ``I'm`` with "unmatched '". The agent
was never prompted at all, so its caller waited for a reply that no one
was writing. Claude Code binds ``!`` and a leading ``/`` the same way; so do
Codex and Vibe. Escaping cannot fix this: the characters are legitimate
prose, and the receiving application is right to bind them.

A paste is the mechanism a terminal already has for "this is text, not
keystrokes". ``paste-buffer -p`` wraps the buffer in bracketed-paste markers
*if the application asked for them* (DECSET 2004) and sends it plain
otherwise, so tmux makes that decision from the receiver's own declared
capability rather than from a guess in a table here. All four supported
CLIs request it, and the real-server tests assert that the markers arrive.

The buffer is named per pane so two concurrent sends cannot paste each
other's text, and deleted on the way out even if the paste fails, so a
dead pane cannot leave the buffer stack growing.

Enter stays a separate ``send-keys``: it is a key, and inside a bracketed
paste it would be inserted as a literal newline instead of submitting.

Verified against a real server by ``tests/test_tmux_rig.py``, which runs a
private tmux (via ``TMUX_TMPDIR``) with a program that logs every byte it
receives.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

from theater.constants.tmux import TMUX_PASTE_BUFFER_PREFIX, TMUX_RAW_PASTE_MIN_VERSION


async def deliver_text(pane_id: str, text: str, *, enter: bool = True) -> None:
    # Resolve from the facade so test patches to client.* are seen.
    from theater.tmux.client import run, tmux_at_least

    buffer = f"{TMUX_PASTE_BUFFER_PREFIX}{pane_id.lstrip('%')}"
    await run("set-buffer", "-b", buffer, "--", text)
    try:
        # tmux 3.7+ escapes pastes via vis(3); -S restores raw bytes (libtmux no_vis).
        paste_args = ["paste-buffer", "-b", buffer, "-t", pane_id, "-p", "-d"]
        if tmux_at_least(*TMUX_RAW_PASTE_MIN_VERSION):
            paste_args.append("-S")
        await run(*paste_args)
    finally:
        await run("delete-buffer", "-b", buffer, check=False)
    if enter:
        await run("send-keys", "-t", pane_id, "Enter")


async def deliver_keys(
    pane_id: str,
    keys: Sequence[str],
    *,
    inter_key_delay_seconds: float | None = None,
) -> None:
    """Deliver a bounded manifest-declared key sequence to a pane."""
    from theater.tmux.client import run

    for index, key in enumerate(keys):
        if index and inter_key_delay_seconds is not None:
            await asyncio.sleep(inter_key_delay_seconds)
        await run("send-keys", "-t", pane_id, key)
