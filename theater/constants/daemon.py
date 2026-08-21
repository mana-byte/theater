"""Immutable daemon RPC timings.

Fixed ceilings and delays that are not user-configurable defaults: a default
is a value the user may override in config.toml, a limit is the wall the
override must stay inside. Kept apart from `theater.config` so a setting's
default and the floor it is measured against are not defined in the same
breath.
"""

from __future__ import annotations

#: Ceiling on a single `jobs.await`; five minutes is longer than any turn observed.
RPC_MAX_AWAIT_SECONDS = 300.0

#: How long an await must block before announcement; read at call time so tests can patch it.
RPC_AWAIT_ANNOUNCE_DELAY_SECONDS = 0.25

#: How long a running send job keeps its exclusive claim on a pane; past this it no longer blocks.
SEND_CLAIM_TTL_SECONDS = 300.0

#: Meta key for the durable send-sequence counter; persisted, never derived from MAX(jobs).
SEND_SEQ_META_KEY = "send_seq"

#: Meta key prefix for per-participant receipt tokens; the participant id is appended.
RECEIPT_TOKEN_PREFIX = "receipt_token:"
