"""Immutable presence-policy constants shared by the tmux and daemon layers."""

from __future__ import annotations

#: Periodic inventory refresh bound; hooks only accelerate it.
PRESENCE_REFRESH_INTERVAL_SECONDS = 2.0

#: Backoff before re-arming the wait-for waiter after a tmux error.
PRESENCE_WAKE_BACKOFF_SECONDS = 1.0

#: Bounded wait for monitor tasks to cancel and reap during aclose.
PRESENCE_CLOSE_TIMEOUT_SECONDS = 5.0

#: A cached inventory older than this fails every snapshot closed.
PRESENCE_INVENTORY_STALE_SECONDS = 5.0

#: Bound for one owned inventory observation, independent of callers.
PRESENCE_REFRESH_TIMEOUT_SECONDS = 5.0

#: Periodic re-check of the focus-events option and wake hook coverage.
PRESENCE_ARM_CHECK_INTERVAL_SECONDS = 10.0

#: Follow-up refresh after a wake: hook bursts settle faster than one pass.
PRESENCE_SETTLE_SECONDS = 0.3

#: Deterministic wake channel; a fixed name lets a restart sweep stale hook entries.
PRESENCE_WAKE_CHANNEL = "theater-presence-wake"

#: tmux option that gates terminal focus reporting for attached clients.
PRESENCE_FOCUS_EVENTS_OPTION = "focus-events"

#: client_flags literal marking a client that currently has terminal focus.
PRESENCE_FLAG_FOCUSED = "focused"

#: termfeatures literal proving this client's terminal reports focus.
PRESENCE_FEATURE_FOCUS = "focus"

#: Hook events whose firing must wake a fresh inventory. after-join-pane and
#: after-break-pane are not settable in tmux 3.7b; window linkage covers them.
PRESENCE_WAKE_HOOK_EVENTS: tuple[str, ...] = (
    "client-focus-in",
    "client-focus-out",
    "pane-focus-in",
    "pane-focus-out",
    "client-attached",
    "client-detached",
    "client-session-changed",
    "client-active",
    "after-select-window",
    "after-select-pane",
    "after-new-window",
    "after-kill-pane",
    "after-split-window",
    "window-linked",
    "window-unlinked",
)
