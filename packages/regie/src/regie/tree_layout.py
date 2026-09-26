"""Persistent local ordering for the Régie participant tree."""

from __future__ import annotations

import json
import re
import secrets
from collections.abc import Collection
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from regie.atomic_file import atomic_write

_VERSION = 1
_SEPARATOR_ID = re.compile(r"sep:[0-9a-f]{8}\Z")


@dataclass(slots=True)
class TreeLayout:
    orders: dict[str, list[str]] = field(default_factory=dict)
    separators: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> tuple[TreeLayout, str | None]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return cls(), None
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            return cls(), f"tree layout ignored: {exc}"
        try:
            return cls._from_wire(value), None
        except (TypeError, ValueError) as exc:
            return cls(), f"tree layout ignored: {exc}"

    @classmethod
    def _from_wire(cls, value: Any) -> TreeLayout:
        if not isinstance(value, dict) or value.get("version") != _VERSION:
            raise ValueError("unsupported or missing version")
        raw_orders = value.get("orders")
        raw_separators = value.get("separators")
        if not isinstance(raw_orders, dict) or not isinstance(raw_separators, dict):
            raise TypeError("orders and separators must be objects")
        orders = _orders_from_wire(raw_orders)
        separators = _separators_from_wire(raw_separators)
        _validate_separator_ownership(orders)
        return cls(orders, separators)

    def save(self, path: Path) -> None:
        value = {
            "version": _VERSION,
            "orders": self.orders,
            "separators": self.separators,
        }
        atomic_write(
            path,
            (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8"),
        )

    def ordered(self, parent_id: str | None, participant_ids: list[str]) -> tuple[str, ...]:
        parent_key = parent_id or ""
        available = set(participant_ids)
        stored = self.orders.get(parent_key, ())
        result = [entry for entry in stored if entry in available or entry in self.separators]
        present = set(result)
        result.extend(entry for entry in participant_ids if entry not in present)
        return tuple(result)

    def move(
        self,
        parent_id: str | None,
        item_id: str,
        offset: int,
        sibling_ids: list[str],
        active_participant_ids: Collection[str],
    ) -> bool:
        parent_key = parent_id or ""
        visible = list(self.ordered(parent_key, sibling_ids))
        if item_id not in visible:
            return False
        index = visible.index(item_id)
        target = index + offset
        if target < 0 or target >= len(visible):
            return False
        other_id = visible[target]
        stored = self._rewritten_order(parent_key, sibling_ids, active_participant_ids)
        for entry in visible:
            if entry not in stored:
                stored.append(entry)
        left, right = stored.index(item_id), stored.index(other_id)
        stored[left], stored[right] = stored[right], stored[left]
        self.orders[parent_key] = stored
        for other_parent, entries in self.orders.items():
            if other_parent != parent_key:
                self.orders[other_parent] = [entry for entry in entries if entry != item_id]
        return True

    def insert_separator(
        self,
        parent_id: str | None,
        above_id: str,
        name: str,
        sibling_ids: list[str],
        active_participant_ids: Collection[str],
    ) -> str:
        cleaned = name.strip()
        if not cleaned:
            raise ValueError("separator name must not be empty")
        parent_key = parent_id or ""
        visible = self.ordered(parent_key, sibling_ids)
        if above_id not in visible:
            raise ValueError("separator target is not in this sibling list")
        stored = self._rewritten_order(parent_key, sibling_ids, active_participant_ids)
        separator_id = self._new_separator_id()
        stored.insert(stored.index(above_id), separator_id)
        self.orders[parent_key] = stored
        self.separators[separator_id] = {"name": cleaned}
        return separator_id

    def rename_separator(self, separator_id: str, name: str) -> bool:
        cleaned = name.strip()
        if separator_id not in self.separators or not cleaned:
            return False
        self.separators[separator_id]["name"] = cleaned
        return True

    def toggle_separator(self, separator_id: str) -> bool:
        """Fold or unfold the section a separator heads; false if it is unknown."""
        record = self.separators.get(separator_id)
        if record is None:
            return False
        if record.get("collapsed"):
            del record["collapsed"]
        else:
            record["collapsed"] = True
        return True

    def delete_separator(self, separator_id: str) -> bool:
        if self.separators.pop(separator_id, None) is None:
            return False
        for parent_id, entries in self.orders.items():
            self.orders[parent_id] = [entry for entry in entries if entry != separator_id]
        return True

    def parent_key_for_separator(self, separator_id: str) -> str | None:
        return next(
            (parent_id for parent_id, entries in self.orders.items() if separator_id in entries),
            None,
        )

    def _rewritten_order(
        self,
        parent_key: str,
        sibling_ids: list[str],
        active_participant_ids: Collection[str],
    ) -> list[str]:
        retained = [
            entry
            for entry in self.orders.get(parent_key, ())
            if entry in self.separators or entry in active_participant_ids
        ]
        return list(dict.fromkeys((*retained, *sibling_ids)))

    def _new_separator_id(self) -> str:
        while (separator_id := f"sep:{secrets.token_hex(4)}") in self.separators:
            pass
        return separator_id


def _orders_from_wire(raw_orders: dict[Any, Any]) -> dict[str, list[str]]:
    orders: dict[str, list[str]] = {}
    for parent_id, entries in raw_orders.items():
        if not isinstance(parent_id, str) or not isinstance(entries, list):
            raise TypeError("each order must map a parent id to a list")
        if not all(isinstance(entry, str) and entry for entry in entries):
            raise TypeError("order entries must be non-empty strings")
        if len(entries) != len(set(entries)):
            raise ValueError("an order contains duplicate ids")
        orders[parent_id] = list(entries)
    return orders


def _separators_from_wire(raw_separators: dict[Any, Any]) -> dict[str, dict[str, Any]]:
    separators: dict[str, dict[str, Any]] = {}
    for separator_id, record in raw_separators.items():
        if not isinstance(separator_id, str) or _SEPARATOR_ID.fullmatch(separator_id) is None:
            raise ValueError("separator ids must be 'sep:' followed by eight lowercase hex digits")
        if not isinstance(record, dict) or not {"name"} <= set(record) <= {"name", "collapsed"}:
            raise TypeError("each separator holds a name and an optional collapsed flag")
        name = record.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("separator names must be non-empty strings")
        if record.get("collapsed", False) is not True and "collapsed" in record:
            raise TypeError("a separator's collapsed flag is either true or absent")
        separators[separator_id] = {
            "name": name.strip(),
            **({"collapsed": True} if "collapsed" in record else {}),
        }
    return separators


def _validate_separator_ownership(orders: dict[str, list[str]]) -> None:
    seen: set[str] = set()
    for entries in orders.values():
        for entry in entries:
            if entry.startswith("sep:") and entry in seen:
                raise ValueError("a separator may belong to only one sibling list")
            if entry.startswith("sep:"):
                seen.add(entry)


__all__ = ["TreeLayout"]
