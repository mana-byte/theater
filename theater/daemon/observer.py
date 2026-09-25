"""Re-export of ``theater.daemon.observation``: status from tailing transcripts agents write.
Needs no agent cooperation (mid-tool-call agents make no MCP calls); attaches at EOF so adopted
history never floods the bus. Only the screen tells AWAITING_INPUT from thinking.
"""

from __future__ import annotations

import time

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
from theater.daemon.observation.live import (  # noqa: F401 — re-exported for lifecycle/test imports
    LiveObservationHub,
    LiveRegistration,
    LiveRegistrationError,
)
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
