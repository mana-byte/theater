"""Compatibility façade for the spawning package.

Re-exports ``Spawner``, ``SpawnRequest``, ``Reservation`` and the
module-level attributes tests historically referenced (``shutil``, ``tmux``,
``FALLBACK_SESSION``). Production code should import from
``theater.daemon.spawning`` directly.
"""

from __future__ import annotations

import shutil

from theater.constants.harness import FALLBACK_SESSION
from theater.daemon.spawning.models import Reservation, SpawnRequest
from theater.daemon.spawning.service import Spawner
from theater.tmux import client as tmux

__all__ = [
    "FALLBACK_SESSION",
    "Reservation",
    "SpawnRequest",
    "Spawner",
    "shutil",
    "tmux",
]
