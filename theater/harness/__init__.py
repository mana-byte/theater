"""Harness registry — compatibility façade.

Every adapter is a plugin file. The ones Theater ships live in
`builtin/plugins/`, the ones a user writes live in `$THEATER_HOME/harnesses/`,
and both are read by `plugins.scan` under the same contract. There is no
built-in tier. Each adapter is two objects: a `Harness` that knows how to launch
the CLI, and the `HarnessObserver` it carries, which knows how to watch it.

`install` turns those files into the live registry. Until it runs the registry
is empty, which is deliberate. Every process that touches the registry
installs first.

Nothing above this package needs to change to add a harness, because nothing
above it sees anything but `Event`.

The registry implementation lives in `theater.harness.registry`. This module
re-exports the exact public API and the private registry objects that tests
and integrations import, so existing imports continue to work unchanged.
"""

from __future__ import annotations

import logging
import shutil  # noqa: F401 — tests monkeypatch theater.harness.shutil.which

from theater.config import ConfigError  # noqa: F401 — tests catch harness.ConfigError
from theater.constants.harness import (  # noqa: F401
    HARNESS_TMUX_OBSERVATION_NAME_LENGTH as _TMUX_TRUNCATION,
)
from theater.harness.base import (
    APPROVALS,
    MAX_TEXT,
    SERVER_NAME,
    Event,
    EventKind,
    EventPath,
    Harness,
    LaunchPlan,
    NativeChild,
    ResumeLaunchOverlay,
    clip,
    clipper,
    last_screen_line,
    status_after,
    theater_binary,
)
from theater.harness.contracts.observation import (
    HarnessObserver,
    ScreenConfidence,
    ScreenKind,
    ScreenReading,
)
from theater.harness.contracts.source import (
    Attachment,
    Batch,
    History,
    Source,
    StreamPoint,
)
from theater.harness.plugins import Plugin, PluginError
from theater.harness.registry import (  # noqa: F401
    _ALIASES,
    _BINARIES,
    _BROKEN,
    _OBSERVATION_KEYS,
    _PLUGINS,
    HARNESSES,
)
from theater.harness.registry.capabilities import (
    check_model,
    check_reasoning,
    check_resume,
    plan_launch,
    supports_model,
    supports_reasoning,
    supports_resume,
)
from theater.harness.registry.claims import (  # noqa: F401
    _observation_keys_for,
)
from theater.harness.registry.claims import (  # noqa: F401
    binary_claim_keys as _binary_claim_keys,
)
from theater.harness.registry.claims import (  # noqa: F401
    claim_alias as _claim_alias,
)
from theater.harness.registry.claims import (  # noqa: F401
    claim_binary as _claim_binary,
)
from theater.harness.registry.claims import (  # noqa: F401
    claim_name as _claim_name,
)
from theater.harness.registry.claims import (  # noqa: F401
    claim_observation_keys as _claim_observation_keys,
)
from theater.harness.registry.claims import (  # noqa: F401
    release_claims as _release_claims,
)
from theater.harness.registry.claims import (  # noqa: F401
    unwrap_binary as _unwrap_binary,
)
from theater.harness.registry.install import (  # noqa: F401
    _reject,
    install,
)
from theater.harness.registry.lookup import (
    UNKNOWN_ICON,
    describe,
    get,
    harness_icon,
    known_binaries,
    normalize,
    observation_lookup,
)
from theater.harness.transcript.observer import TranscriptObserver
from theater.harness.transcript.source import TranscriptSource

logger = logging.getLogger("theater.harness")

__all__ = [
    "APPROVALS",
    "HARNESSES",
    "MAX_TEXT",
    "SERVER_NAME",
    "UNKNOWN_ICON",
    "Attachment",
    "Batch",
    "Event",
    "EventKind",
    "EventPath",
    "Harness",
    "HarnessObserver",
    "History",
    "LaunchPlan",
    "NativeChild",
    "Plugin",
    "PluginError",
    "ResumeLaunchOverlay",
    "ScreenConfidence",
    "ScreenKind",
    "ScreenReading",
    "Source",
    "StreamPoint",
    "TranscriptObserver",
    "TranscriptSource",
    "check_model",
    "check_reasoning",
    "check_resume",
    "clip",
    "clipper",
    "describe",
    "get",
    "harness_icon",
    "install",
    "known_binaries",
    "last_screen_line",
    "normalize",
    "observation_lookup",
    "plan_launch",
    "status_after",
    "supports_model",
    "supports_reasoning",
    "supports_resume",
    "theater_binary",
]
