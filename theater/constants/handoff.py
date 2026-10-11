"""Bounds and vocabulary for durable ``after`` handoffs."""

from __future__ import annotations

from typing import Final

TASK_KIND_WORK: Final = "work"
TASK_KIND_REVIEW: Final = "independent_review"
TASK_KINDS: Final = (TASK_KIND_WORK, TASK_KIND_REVIEW)

#: Gate lifecycle: waiting for the source, launching the child, launched, or never launched.
GATE_WAITING: Final = "waiting"
GATE_OPENING: Final = "opening"
GATE_OPENED: Final = "opened"
GATE_FAILED: Final = "failed"
GATE_CANCELLED: Final = "cancelled"

#: Whether a source's pinned Git state can be reviewed exactly.
PIN_PINNED: Final = "pinned"
PIN_DIRTY: Final = "dirty"
PIN_UNPINNED: Final = "unpinned"

ANSWER_MAX_CHARS: Final = 16_384
TASK_MAX_CHARS: Final = 2_048
PATHS_MAX: Final = 20
#: Hard bound on the Git subprocesses that pin a source at completion.
GIT_PIN_TIMEOUT_SECONDS: Final = 3
GIT_DIRTY_OUTPUT_MAX_BYTES: Final = 65_536
#: Open gates one caller may hold, and the longest chain of gated sources followed.
MAX_WAITING_PER_CALLER: Final = 16
MAX_CHAIN: Final = 16

ERROR_SOURCE_NOT_REVIEWABLE: Final = "source_not_reviewable"
ERROR_SOURCE_GONE: Final = "handoff_source_gone"
ERROR_LAUNCH_REFUSED: Final = "handoff_launch_refused"
ERROR_CANCELLED: Final = "cancelled"
