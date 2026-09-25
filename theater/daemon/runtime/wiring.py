"""Native runtime wiring selection and launch-generation facts.

Harness-name-free (manifest ``runtime`` + compatibility probe). ``auto``/``native`` fall
back to legacy, ``legacy`` opts out, and existing participants keep their persisted wiring.
"""

from __future__ import annotations

import logging

from theater.daemon.harness_runtime.backend import backend_artifacts_dir
from theater.harness.contracts.runtime import RuntimeManifest

logger = logging.getLogger("theater.daemon.runtime")

#: Disabling this makes future ``auto`` selections legacy; explicit ``native``
#: remains a preference subject to the harness compatibility probe.
NATIVE_AUTO_SELECTION_ENABLED = True

#: One backend generation per fresh launch. A binding row is the current
#: generation's record; resume/fork spawns create a new participant with its
#: own backend, so a fresh binding always starts at generation 1.
RUNTIME_BACKEND_GENERATION_INITIAL = 1

#: The private backend socket file name inside the participant's runtime dir.
BACKEND_SOCKET_NAME = "backend.sock"
FRONTEND_SOCKET_NAME = "frontend.sock"


def native_endpoint(participant_id: str) -> str:
    """The private ``unix:///abs/path`` WebSocket endpoint for one participant's backend.

    The directory is created 0o700 and symlink-checked by the backend-artifact module.
    """
    return f"unix://{backend_artifacts_dir(participant_id) / BACKEND_SOCKET_NAME}"


def frontend_endpoint(participant_id: str) -> str:
    return f"unix://{backend_artifacts_dir(participant_id) / FRONTEND_SOCKET_NAME}"


def runtime_manifest_of(harness) -> RuntimeManifest | None:
    """The compiled harness's runtime manifest, or ``None`` for legacy harnesses.

    Manifests predating runtime wiring have ``runtime=None``, so they are legacy by construction.
    """
    manifest = getattr(harness, "runtime", None)
    return manifest if isinstance(manifest, RuntimeManifest) else None
