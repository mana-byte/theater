"""Values shared by multiple subsystems.

Subsystem-local constants stay beside their consumer; this module is only for
numeric contracts that otherwise acquire competing definitions across layers.
"""

# SQLite timestamps and retention cutoffs use seconds; float keeps SQL division real.
SECONDS_PER_DAY = 86_400.0

# The régie's average-cost footer examines this rolling number of days.
USAGE_AVERAGE_WINDOW_DAYS = 30

# Legacy usage RPC windows are expressed in hours rather than seconds.
USAGE_AVERAGE_WINDOW_HOURS = USAGE_AVERAGE_WINDOW_DAYS * 24.0

# Costs are persisted as one hundred-millionth-of-a-dollar units.
MICROCENTS_PER_DOLLAR = 100_000_000
