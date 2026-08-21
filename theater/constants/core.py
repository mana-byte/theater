"""Cross-layer naming rules.

A harness name is a spawn argument, a wire value, and part of a tmux window
name; a plugin harness names itself in Python and must meet the same rule, so
the rule is stated once here and imported by both config validation and the
harness plugin loader rather than restated in two places that could disagree.
"""

from __future__ import annotations

import re

#: Legal harness name: lowercase letters, digits, '-' or '_', starting alphanumeric.
HARNESS_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
