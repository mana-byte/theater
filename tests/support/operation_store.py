"""The operation and journal repositories over one database, as the operation services take them."""

from __future__ import annotations

from pathlib import Path

from theater.daemon.persistence.database import Database
from theater.daemon.persistence.repositories.journal import JournalRepository
from theater.daemon.persistence.repositories.operations import OperationRepository


class OperationStore:
    def __init__(self, path: Path) -> None:
        self.db = Database(path)
        self.operations = OperationRepository(self.db)
        self.journal = JournalRepository(self.db)

    def write_unit(self):
        return self.db.write_unit()

    def close(self) -> None:
        self.db.close()
