"""Compatibility re-export for the persistence package.

The Store façade, Database owner, and repositories live in
``theater.daemon.persistence``. This module re-exports the symbols that
existing imports depend on: ``Store``, ``HEAD``, ``MIGRATIONS``,
``BASELINE``, ``RECEIPT_TOKEN_PREFIX``, and the ``participants`` schema table.
"""

from __future__ import annotations

from theater.daemon.persistence.database import BASELINE, HEAD, MIGRATIONS
from theater.daemon.persistence.repositories.receipts import RECEIPT_TOKEN_PREFIX
from theater.daemon.persistence.store import Store
from theater.daemon.schema import participants

__all__ = ["BASELINE", "HEAD", "MIGRATIONS", "RECEIPT_TOKEN_PREFIX", "Store", "participants"]
