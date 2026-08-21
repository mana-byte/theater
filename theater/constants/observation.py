"""Immutable observer constants that are not user-configurable defaults.

Configurable polling/search/screen/rescue settings stay in ``config.ObserverSection``;
the values here are fixed sentinels and caps the user cannot override.
"""

from __future__ import annotations

#: Marks a job the observer finished without ever reading a turn-end record.
#: Salvage, not a reply the harness declared complete.
RESCUE_CODE = "turn_end_unseen"

#: Consecutive idle-looking screens before a turn is called finished. Two, not
#: one: a harness that clears the pane between phases shows a bare prompt mid-work.
IDLE_CONFIRMATIONS = 2

#: How many handled turn ids one participant remembers. Duplicates are adjacent
#: (Claude writes them as consecutive records), so a small window suffices.
ANSWERED_TURNS = 32

#: How much of a prompt has to reappear before a turn is called an answer to it.
#: Long enough to be specific, short enough that the clip point is nowhere near it.
PROMPT_MATCH = 120

#: Consecutive relocate/evidence windows that must agree before identity-loss
#: quarantine is entered. One window alone is a transient scan artifact.
IDENTITY_LOSS_CONFIRMATIONS = 2

#: How many consecutive turn ends that do not match the waiting job's prompt
#: are tolerated before the job is released.
UNMATCHED_LIMIT = 2

#: How many entries the per-job miss counter holds before the oldest is evicted.
UNMATCHED_CAP = 256

#: Error code for a job released because its prompt was never seen in the transcript.
UNDELIVERED_CODE = "prompt_never_seen"

#: Grace window (seconds) before a source failure crashes a running job.
OBSERVATION_FAILURE_GRACE = 30.0

#: Error code for a transcript correlation that cannot be uniquely attributed.
CORRELATION_AMBIGUOUS_CODE = "transcript_correlation_ambiguous"

#: Sentinel for "no raw result was provided" in _finish calls.
RAW_RESULT_UNSET = object()

#: Log format for a watcher retired by a source contract failure.
SOURCE_CONTRACT_FAILED = "source contract failed for %s; retiring watcher"
