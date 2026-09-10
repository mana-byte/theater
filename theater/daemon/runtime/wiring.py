"""Native runtime wiring selection and launch-generation facts.

The composition seam between spawn surfaces and the frozen runtime contracts.
Nothing here knows a harness name: selection reads the harness manifest's
``runtime`` declaration and the Theater-verified compatibility probe, so a
local plugin without a runtime manifest keeps legacy wiring exactly as
before.

Auto selection is gated by :data:`NATIVE_AUTO_SELECTION_ENABLED` — the plan's
Wave 5 release gate. Until that gate passes, ``wiring="auto"`` (the internal
default on spawn surfaces) selects legacy for every harness; the selection
logic below is complete and exercised by the lifecycle tests with the gate
enabled, so flipping the constant is the whole rollout.
"""

from __future__ import annotations

import logging

from theater.daemon.harness_runtime.backend import backend_artifacts_dir
from theater.harness.contracts.runtime import RuntimeManifest

logger = logging.getLogger("theater.daemon.runtime")

#: The Wave 5 release gate. Automatic native selection stays disabled until
#: the integrated pilot validation passes; flipping this constant is the
#: entire rollout decision. Explicit ``wiring="legacy"`` and explicit internal
#: ``wiring="native"`` selections are honoured regardless of this gate.
NATIVE_AUTO_SELECTION_ENABLED = False

#: One backend generation per fresh launch. A binding row is the current
#: generation's record; resume/fork spawns create a new participant with its
#: own backend, so a fresh binding always starts at generation 1.
RUNTIME_BACKEND_GENERATION_INITIAL = 1

#: The private backend socket file name inside the participant's runtime dir.
BACKEND_SOCKET_NAME = "backend.sock"


def native_endpoint(participant_id: str) -> str:
    """The private local WebSocket endpoint for one participant's backend.

    The canonical ``unix:///abs/path`` form: the runtime engine accepts it
    directly and the native CLI renders it as ``unix://<path>`` for
    ``--listen``/``--remote``. The directory is created private (0o700) and
    symlink-checked by the backend-artifact ownership module.
    """
    return f"unix://{backend_artifacts_dir(participant_id) / BACKEND_SOCKET_NAME}"


def runtime_manifest_of(harness) -> RuntimeManifest | None:
    """The compiled harness's runtime manifest, or ``None`` for legacy harnesses.

    ``HarnessManifest.runtime`` is ``None`` for every manifest that predates
    runtime wiring, so a local plugin overriding a shipped one without a
    ``runtime`` field is legacy by construction.
    """
    manifest = getattr(harness, "runtime", None)
    return manifest if isinstance(manifest, RuntimeManifest) else None
