"""Native runtime wiring selection and launch-generation facts.

The composition seam between spawn surfaces and the frozen runtime contracts.
Nothing here knows a harness name: selection reads the harness manifest's
``runtime`` declaration and the Theater-verified compatibility probe, so a
local plugin without a runtime manifest keeps legacy wiring exactly as
before.

Auto selection is gated by :data:`NATIVE_AUTO_SELECTION_ENABLED` — the plan's
Wave 5 release gate, now passed: the integrated pilot validation, the
production daemon smoke, and the stock-release native proofs verified the
auto rollout end to end. With the verified rollout enabled,
``wiring="auto"`` (the default on spawn surfaces) selects native only for
Theater-verified-compatible NEW spawns on the pinned verified stock release;
unknown or unsupported versions and harnesses without a runtime manifest
keep legacy. Flipping the constant back is the whole rollback: future auto
spawns select legacy, and live participants stay pinned to their persisted
wiring.
"""

from __future__ import annotations

import logging

from theater.daemon.harness_runtime.backend import backend_artifacts_dir
from theater.harness.contracts.runtime import RuntimeManifest

logger = logging.getLogger("theater.daemon.runtime")

#: The Wave 5 release gate, passed at the verified integrated base: the
#: verified auto rollout is enabled, so ``auto`` selects native only for
#: Theater-verified-compatible NEW spawns on the pinned verified stock
#: release. Flipping this constant back is the entire rollback decision —
#: future auto spawns select legacy, and live participants stay pinned to
#: their persisted wiring. Explicit ``wiring="legacy"`` and explicit internal
#: ``wiring="native"`` selections are honoured regardless of this gate.
NATIVE_AUTO_SELECTION_ENABLED = True

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
