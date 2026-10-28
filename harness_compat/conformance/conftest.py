"""Collect the live scenarios only inside the pipeline sandbox, never in a development run."""

from __future__ import annotations

import os

from harness_compat.process import SANDBOX_ENV

collect_ignore = [] if os.environ.get(SANDBOX_ENV) else ["live"]
