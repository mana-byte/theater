"""Compatibility façade for the spawning package.

Re-exports ``Spawner``, ``SpawnRequest``, and ``Reservation``. Production code
should import from ``theater.daemon.spawning`` directly.
"""

from __future__ import annotations

import shutil

from theater.daemon.spawning.models import Reservation, SpawnRequest
from theater.daemon.spawning.service import Spawner

__all__ = [
    "Reservation",
    "SpawnRequest",
    "Spawner",
    "shutil",
]
