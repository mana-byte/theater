"""Foundational validation floors and ceilings, not user-configurable defaults.
Kept apart from ``theater.config`` so a default and the floor it is measured against never share a
definition.
"""

from __future__ import annotations

#: Below this the daemon spends more time waking up than working; 0.0001 spins a core.
MIN_INTERVAL = 0.01

#: Maximum Unicode codepoints in a participant's durable description.
PARTICIPANT_DESCRIPTION_MAX_CODEPOINTS = 160
