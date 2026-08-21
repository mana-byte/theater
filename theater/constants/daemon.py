"""Immutable daemon RPC timings.

Fixed ceilings and delays that are not user-configurable defaults: a default
is a value the user may override in config.toml, a limit is the wall the
override must stay inside. Kept apart from `theater.config` so a setting's
default and the floor it is measured against are not defined in the same
breath.
"""

from __future__ import annotations

#: Ceiling on a single `jobs.await`; five minutes is longer than any turn observed.
MAX_AWAIT = 300.0

#: How long an await must block before announcement; read at call time so tests can patch it.
AWAIT_ANNOUNCE_AFTER = 0.25

#: How long a running send job keeps its exclusive claim on a pane; past this it no longer blocks.
SEND_CLAIM_TTL = 300.0
