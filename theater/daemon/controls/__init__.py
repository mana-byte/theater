"""The daemon control service: durable control state machine for all wiring.

Composition root (Waves 3–4) wires ``Store``, ``JobManager``, one
``HarnessRuntime`` provider, and the ``ControlGates`` seams; the service
itself is harness-neutral and owns only authorization ordering, idle checks,
job correlation, the followup queue, and delivery recovery.
"""

from theater.daemon.controls.gates import ControlGates
from theater.daemon.controls.service import (
    AMBIGUOUS_DELIVERY_DEADLINE_SECONDS,
    ControlService,
    InterruptOutcome,
    QueueDispatchOutcome,
    SettingsOutcome,
)

__all__ = [
    "AMBIGUOUS_DELIVERY_DEADLINE_SECONDS",
    "ControlGates",
    "ControlService",
    "InterruptOutcome",
    "QueueDispatchOutcome",
    "SettingsOutcome",
]
