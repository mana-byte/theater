"""Compatibility imports for control-owned busy-send redirection."""

from theater.daemon.controls.redirect import (
    IF_BUSY_MODES,
    REPLACE_IDLE_WAIT_SECONDS,
    ReplaceTimeout,
    amend,
    authorize_redirect,
    if_busy_param,
    queue,
    replace,
    require_active_job,
)
from theater.daemon.skill_attachments import caller_name as _caller_name

__all__ = [
    "IF_BUSY_MODES",
    "REPLACE_IDLE_WAIT_SECONDS",
    "ReplaceTimeout",
    "_caller_name",
    "amend",
    "authorize_redirect",
    "if_busy_param",
    "queue",
    "replace",
    "require_active_job",
]
