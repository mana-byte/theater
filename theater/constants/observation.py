"""Immutable observer constants that are not user-configurable defaults.

Configurable polling/search/screen/rescue settings stay in config.ObserverSection.
"""

from __future__ import annotations

#: Salvage, not a reply the harness declared complete.
RESCUE_CODE = "turn_end_unseen"

#: Two, not one: a harness that clears the pane mid-work shows a bare prompt for one frame.
IDLE_CONFIRMATIONS = 2

#: Duplicates are adjacent, so a small window suffices.
ANSWERED_TURNS = 32

#: Long enough to be specific, short enough that the clip point is nowhere near it.
PROMPT_MATCH = 120

#: One window alone is a transient scan artifact.
IDENTITY_LOSS_CONFIRMATIONS = 2

#: Consecutive turn ends that do not match the waiting prompt before the job is released.
UNMATCHED_LIMIT = 2

#: Entries the per-job miss counter holds before the oldest is evicted.
UNMATCHED_CAP = 256

#: Job released because its prompt was never seen after UNMATCHED_LIMIT turn ends.
UNDELIVERED_CODE = "prompt_never_seen"

#: Grace window (seconds) before a source failure crashes a running job.
OBSERVATION_FAILURE_GRACE = 30.0

#: Transcript correlation that cannot be uniquely attributed.
CORRELATION_AMBIGUOUS_CODE = "transcript_correlation_ambiguous"

#: Sentinel for "no raw result was provided" in _finish calls.
RAW_RESULT_UNSET = object()

#: Log format for a watcher retired by a source contract failure.
SOURCE_CONTRACT_FAILED = "source contract failed for %s; retiring watcher"
