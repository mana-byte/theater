"""A terminal program whose only job is to record what tmux sends it.

Every claim Theater makes about delivery is a claim about bytes arriving at a
pty: that a paste is bracketed, that Enter is a separate key, that `!` and a
leading `/` reach the application as text instead of firing its keybindings.
None of that can be checked by asserting argv, and none of it was checked at
all before this file existed -- `deliver_text` was written from the tmux manual
and verified by watching a real agent answer, which is a slow and lossy oracle.

So: a stand-in for a CLI. It puts the tty in raw mode, declares DECSET 2004,
and appends every byte it reads to a log file. Raw mode matters twice over --
the kernel must not translate CR, and it must not turn 0x03 into SIGINT, or the
log would show what the terminal discipline decided rather than what tmux
wrote.

The readiness marker follows DECSET 2004 in the same flush. Once `capture-pane`
shows the marker, tmux has consumed the declaration and the program is ready.
The byte log then proves whether `paste-buffer -p` added the bracket markers.

`--modal-on` paints a marker on the screen when a given substring arrives. That
is for the screen-reading work in a later phase, where a harness must be told
apart from an approval dialog; here it only proves `capture-pane` sees what the
pane drew.

Run it under a shell (`sh -c '... ; exec sh'`) to reproduce the dead-CLI shape:
the program exits, the pane survives, and a shell is left at the prompt.
"""

from __future__ import annotations

import argparse
import os
import sys
import termios
import tty
from pathlib import Path

#: Ctrl-C. In raw mode ISIG is off, so this arrives as a byte rather than a
#: signal, which makes it usable as a quit marker a test can type on purpose.
QUIT = b"\x03"

READY = "RIG READY"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", required=True, help="file to append received bytes to")
    parser.add_argument(
        "--modal-on",
        default=None,
        help="substring that makes the program paint a modal marker",
    )
    args = parser.parse_args()

    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    tty.setraw(fd)

    # DECSET precedes the marker in one flush, so the marker confirms the declaration.
    sys.stdout.write("\x1b[?2004h")
    sys.stdout.write(READY + "\r\n")
    sys.stdout.flush()

    # Held open across the read loop and closed in the `finally` below, so a
    # `with` block would have to wrap the whole function body.
    log = Path(args.log).open("ab", buffering=0)  # noqa: SIM115
    needle = args.modal_on.encode() if args.modal_on else None
    try:
        while True:
            chunk = os.read(fd, 4096)
            if not chunk:
                break
            log.write(chunk)
            if needle and needle in chunk:
                sys.stdout.write("\r\n== MODAL ==\r\n")
                sys.stdout.flush()
            if QUIT in chunk:
                break
    finally:
        log.close()
        sys.stdout.write("\x1b[?2004l")
        sys.stdout.flush()
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
