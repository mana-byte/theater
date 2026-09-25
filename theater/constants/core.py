"""Cross-layer naming rules, stated once for config validation and the harness plugin loader."""

from __future__ import annotations

import re

#: Legal harness name: lowercase letters, digits, '-' or '_', starting alphanumeric.
HARNESS_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
