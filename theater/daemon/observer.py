"""Watch what agents are doing by tailing the transcripts they already write.

Why tailing and not instrumentation
-----------------------------------
The alternative was to require each agent to report its own activity over MCP.
That fails for the case Theater exists to serve: an agent that is mid-tool-call
is not making MCP calls, which is exactly when you most want to know what it is
doing. Worse, it would only work for agents cooperative enough to call us, so
an adopted session started before Theater existed would stay invisible.

Both harnesses already write a complete, append-only record of every turn to
disk. Reading it needs no cooperation from the agent, works identically for
spawned and adopted participants, and cannot be forgotten by a harness author.

Getting the text, and deciding what it means
--------------------------------------------
Those are two jobs, and only the second one lives here. The first belongs to
the adapter: a ``HarnessObserver`` (harness/observation.py) opens a ``Source``
(harness/source.py) that produces batches of events for one participant, and
every harness that appends JSONL gets the default file-tailing one without
saying so. This module owns what happens next: the quiet timers, the status
policy, job completion and rescue, and every write to the registry and the bus.
An adapter whose output is not a file writes its own ``Source`` and inherits all
of that unchanged, which is the point — the policy below is where every
observation bug in this project has been, and it is not going to be
reimplemented per adapter.

Note what this module is handed: a ``HarnessObserver``, never a ``Harness``. It
needs nothing from an adapter but how to watch it, and holding only that half
keeps the launch path from drifting into the observe path.

Attach at EOF, always
---------------------
A participant we adopt may have a 3 MB transcript behind it. Replaying that
onto the bus would flood it with history that is neither live nor interesting.
So attaching skips to the current end of file and counts the records it
skipped, so record indices stay true. For a freshly spawned agent the file is
empty and the rule costs nothing — one behaviour, no special case.

What the screen adds
---------------------
IDLE and WORKING are derivable from the transcript alone; AWAITING_INPUT is
not, because a permission modal and a thinking agent produce the same silence.
``_check_idle_screen`` reads the rendered screen via the harness's
``screen_reading`` and maps it to a status: APPROVAL/TRUST settles
AWAITING_INPUT, WORKING settles WORKING, PROMPT settles IDLE, and UNKNOWN
leaves the status untouched. The reducer acts on this reading regardless of
confidence — being wrong here costs a mislabel in the display, which is
cheaper than the unrecoverable cost a send gate would pay for the same
mistake, so the two consumers use different confidence thresholds.

That check runs on both paths through the watch loop, and it has to. A source
that has not attached yet reports ``waiting`` rather than silence, and the
waiting path used to skip every timer — so a harness that writes no transcript
until its first message (Claude) had no status channel at all before it was
first prompted, and sat at the IDLE its spawn set. ``_screen_only`` runs the
screen arm there, and only that arm; see its docstring for why the other two
are not merely unnecessary but wrong.

This module is a compatibility facade. The implementation lives in
``theater.daemon.observation`` (service, reducer, turns, screen, identity,
completion, failures, attachment). Tests monkeypatch module-level names
(``OBSERVATION_FAILURE_GRACE``, ``wall_now``, ``open_participant_source``)
on this module; the implementation reads them at call-time via the facade.
"""

from __future__ import annotations

import time

# Constants that tests monkeypatch at call-time on this module.
from theater.constants.observation import (
    ANSWERED_TURNS as _ANSWERED_TURNS,  # noqa: F401 — re-exported for test imports
)
from theater.constants.observation import (
    CORRELATION_AMBIGUOUS_CODE,
    IDENTITY_LOSS_CONFIRMATIONS,
    IDLE_CONFIRMATIONS,
    OBSERVATION_FAILURE_GRACE,
    RESCUE_CODE,
    UNDELIVERED_CODE,
    UNMATCHED_CAP,
    UNMATCHED_LIMIT,
)
from theater.constants.observation import (
    PROMPT_MATCH as _PROMPT_MATCH,  # noqa: F401 — re-exported for test imports
)
from theater.constants.observation import (
    RAW_RESULT_UNSET as _RAW_RESULT_UNSET,  # noqa: F401 — re-exported for test imports
)
from theater.constants.observation import (
    SOURCE_CONTRACT_FAILED as _SOURCE_CONTRACT_FAILED,  # noqa: F401 — re-exported for test imports
)
from theater.daemon.observation.identity import history_correlation_is_ambiguous
from theater.daemon.observation.reducer import QuietClock
from theater.daemon.observation.screen import screen_result

# Re-export the Observer class and all public symbols.
from theater.daemon.observation.service import (
    AWAITING_INPUT_TIMEOUT,
    POLL_INTERVAL,
    RELOCATE_TIMEOUT,
    RESCUE_TIMEOUT,
    SCREEN_INTERVAL,
    SEARCH_INTERVAL,
    SYNC_INTERVAL,
    Observer,
)
from theater.daemon.observation.turns import Turn, TurnAccumulator, answers_prompt
from theater.harness.transcript.observer import open_participant_source

# Imported here so tests can monkeypatch them; the service reads them at call-time via the facade.
from theater.models import now as wall_now

__all__ = [
    "AWAITING_INPUT_TIMEOUT",
    "CORRELATION_AMBIGUOUS_CODE",
    "IDENTITY_LOSS_CONFIRMATIONS",
    "IDLE_CONFIRMATIONS",
    "OBSERVATION_FAILURE_GRACE",
    "POLL_INTERVAL",
    "RELOCATE_TIMEOUT",
    "RESCUE_CODE",
    "RESCUE_TIMEOUT",
    "SCREEN_INTERVAL",
    "SEARCH_INTERVAL",
    "SYNC_INTERVAL",
    "UNDELIVERED_CODE",
    "UNMATCHED_CAP",
    "UNMATCHED_LIMIT",
    "Observer",
    "QuietClock",
    "Turn",
    "TurnAccumulator",
    "answers_prompt",
    "history_correlation_is_ambiguous",
    "open_participant_source",
    "screen_result",
    "time",
    "wall_now",
]
