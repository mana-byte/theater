"""Foundational validation floors and ceilings.

Hard bounds that are not user-configurable defaults: a default is a value the
user may override, a limit is the wall the override must stay inside. Kept apart
from `theater.config` so a setting's default and the floor it is measured
against are not defined in the same breath. Currently only the minimum
interval, which every timed setting shares.
"""

from __future__ import annotations

#: Below this the daemon spends more time waking up than working; 0.0001 spins a core.
MIN_INTERVAL = 0.01

#: Maximum Unicode codepoints in a participant's durable description.
PARTICIPANT_DESCRIPTION_MAX_CODEPOINTS = 160
