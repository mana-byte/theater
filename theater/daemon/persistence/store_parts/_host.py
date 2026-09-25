"""Typing-only declaration of the ``Store`` state and helpers the mixins share."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

    from theater.daemon.persistence.repositories.artifacts import ArtifactRepository
    from theater.daemon.persistence.repositories.bus import BusRepository
    from theater.daemon.persistence.repositories.channels import ChannelCredentialRepository
    from theater.daemon.persistence.repositories.control_operations import (
        ControlOperationRepository,
    )
    from theater.daemon.persistence.repositories.jobs import JobRepository
    from theater.daemon.persistence.repositories.journal import JournalRepository
    from theater.daemon.persistence.repositories.mcp_plugins import (
        McpPluginCredentialRepository,
    )
    from theater.daemon.persistence.repositories.metadata import MetadataRepository
    from theater.daemon.persistence.repositories.native_evidence import (
        NativeTerminalEvidenceRepository,
    )
    from theater.daemon.persistence.repositories.participants import ParticipantRepository
    from theater.daemon.persistence.repositories.receipts import ReceiptRepository
    from theater.daemon.persistence.repositories.runtime_bindings import (
        RuntimeBindingRepository,
    )
    from theater.daemon.persistence.repositories.scratchpad import ScratchpadRepository
    from theater.daemon.persistence.repositories.statistics import StatisticsRepository
    from theater.daemon.persistence.repositories.usage import UsageRepository
    from theater.daemon.persistence.repositories.worktrees import WorktreeRepository
    from theater.daemon.persistence.transactions import SQLiteWriteUnit

BusListener = Callable[[dict], None]


class StoreHost:
    """Attributes ``Store.__init__`` assigns and cross-mixin helpers; no runtime members."""

    if TYPE_CHECKING:
        engine: Engine
        journal: JournalRepository
        _participants: ParticipantRepository
        _artifacts: ArtifactRepository
        _jobs: JobRepository
        _bus: BusRepository
        _meta: MetadataRepository
        _receipts: ReceiptRepository
        _channels: ChannelCredentialRepository
        _mcp_plugins: McpPluginCredentialRepository
        _scratchpad: ScratchpadRepository
        _worktrees: WorktreeRepository
        _usage: UsageRepository
        _statistics: StatisticsRepository
        _runtime_bindings: RuntimeBindingRepository
        _control_operations: ControlOperationRepository
        _native_evidence: NativeTerminalEvidenceRepository
        _bus_listeners: list[BusListener]

        def write_unit(self) -> SQLiteWriteUnit: ...

        @staticmethod
        def _bus_row(
            row_id: int,
            timestamp: float,
            from_id: str | None,
            to_id: str | None,
            kind: str,
            payload_text: str | None,
        ) -> dict: ...

        def _notify_bus_listeners(
            self, rows: list[dict], listeners: tuple[BusListener, ...]
        ) -> None: ...

        def _append_participant_controls_event(
            self, unit, participant_id: str, *, recorded_at: float
        ) -> None: ...

        def _append_control_event(self, unit, operation) -> None: ...
