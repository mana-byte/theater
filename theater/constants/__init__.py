"""Values shared by multiple subsystems.

Subsystem-local constants stay beside their consumer; this package is only for
constants that otherwise acquire competing definitions across layers: the
numeric contracts below (time, cost, usage windows) and the harness naming
rule and validation floor re-exported from `core` and `limits`.
"""

from theater.constants.core import HARNESS_NAME
from theater.constants.limits import MIN_INTERVAL

# SQLite timestamps and retention cutoffs use seconds; float keeps SQL division real.
SECONDS_PER_DAY = 86_400.0

# The régie's average-cost footer examines this rolling number of days.
USAGE_AVERAGE_WINDOW_DAYS = 30

# Legacy usage RPC windows are expressed in hours rather than seconds.
USAGE_AVERAGE_WINDOW_HOURS = USAGE_AVERAGE_WINDOW_DAYS * 24.0

# Costs are persisted as one hundred-millionth-of-a-dollar units.
MICROCENTS_PER_DOLLAR = 100_000_000

__all__ = [
    "HARNESS_NAME",
    "MICROCENTS_PER_DOLLAR",
    "MIN_INTERVAL",
    "SECONDS_PER_DAY",
    "USAGE_AVERAGE_WINDOW_DAYS",
    "USAGE_AVERAGE_WINDOW_HOURS",
]
