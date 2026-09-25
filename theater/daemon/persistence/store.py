"""Store compatibility façade composing repositories over one Database."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from theater.daemon.persistence.database import Database
from theater.daemon.persistence.repositories.artifacts import ArtifactRepository
from theater.daemon.persistence.repositories.bus import BusRepository
from theater.daemon.persistence.repositories.channels import ChannelCredentialRepository
from theater.daemon.persistence.repositories.control_operations import (
    ControlOperationRepository,
)
from theater.daemon.persistence.repositories.jobs import JobRepository
from theater.daemon.persistence.repositories.journal import JournalRepository
from theater.daemon.persistence.repositories.mcp_plugins import McpPluginCredentialRepository
from theater.daemon.persistence.repositories.metadata import MetadataRepository
from theater.daemon.persistence.repositories.native_evidence import (
    NativeTerminalEvidenceRepository,
)
from theater.daemon.persistence.repositories.operations import OperationRepository
from theater.daemon.persistence.repositories.participants import ParticipantRepository
from theater.daemon.persistence.repositories.providers import ProviderRepository
from theater.daemon.persistence.repositories.receipts import ReceiptRepository
from theater.daemon.persistence.repositories.runtime_bindings import RuntimeBindingRepository
from theater.daemon.persistence.repositories.scratchpad import ScratchpadRepository
from theater.daemon.persistence.repositories.statistics import StatisticsRepository
from theater.daemon.persistence.repositories.terminal_bindings import TerminalBindingRepository
from theater.daemon.persistence.repositories.usage import UsageRepository
from theater.daemon.persistence.repositories.workspaces import WorkspaceRepository
from theater.daemon.persistence.repositories.worktrees import WorktreeRepository
from theater.daemon.persistence.store_parts import (
    BusStore,
    ControlOperationStore,
    CredentialStore,
    JobStore,
    ParticipantStore,
    RuntimeBindingStore,
    ScratchpadStore,
    UsageStore,
)
from theater.daemon.persistence.store_parts._host import BusListener
from theater.daemon.persistence.transactions import SQLiteWriteUnit

logger = logging.getLogger("theater.store")

__all__ = ["BusListener", "Store"]


class Store(
    ParticipantStore,
    JobStore,
    CredentialStore,
    ScratchpadStore,
    UsageStore,
    RuntimeBindingStore,
    ControlOperationStore,
    BusStore,
):
    """Compatibility façade over ``Database`` and explicit repositories."""

    def __init__(self, path: Path):
        self._db = Database(path)
        self.path = self._db.path
        self.engine = self._db.engine
        self.conn = self._db.conn

        self._participants = ParticipantRepository(self._db)
        self._artifacts = ArtifactRepository(self._db)
        self._jobs = JobRepository(self._db)
        self._bus = BusRepository(self._db)
        self._meta = MetadataRepository(self._db)
        self._receipts = ReceiptRepository(self._db, self._meta, self._participants)
        self._channels = ChannelCredentialRepository(self._db, self._meta, self._participants)
        self._mcp_plugins = McpPluginCredentialRepository(self._db, self._participants)
        self._scratchpad = ScratchpadRepository(self._db)
        self._worktrees = WorktreeRepository(self._db)
        self._usage = UsageRepository(self._db)
        self._statistics = StatisticsRepository(self._db)
        self._runtime_bindings = RuntimeBindingRepository(self._db)
        self._control_operations = ControlOperationRepository(self._db)
        self._native_evidence = NativeTerminalEvidenceRepository(self._db)
        self.providers = ProviderRepository(self._db)
        self.terminal_bindings = TerminalBindingRepository(self._db)
        self.operations = OperationRepository(self._db)
        self.workspaces = WorkspaceRepository(self._db)
        self.journal = JournalRepository(self._db)
        self._bus_listeners: list[BusListener] = []
        self._participant_name_resolver: Callable[[str], str | None] | None = None

    def close(self) -> None:
        self._bus_listeners.clear()
        self._db.close()

    def write_unit(self) -> SQLiteWriteUnit:
        """Create one short transaction shared by cooperating repositories."""
        return self._db.write_unit()

    def set_participant_name_resolver(self, resolver: Callable[[str], str | None]) -> None:
        """Install the daemon's in-memory public-name lookup."""
        self._participant_name_resolver = resolver

    def participant_projection_name(self, participant_id: str) -> str | None:
        resolver = self._participant_name_resolver
        return None if resolver is None else resolver(participant_id)
