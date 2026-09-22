"""Action display deduplication and reconciliation state, independent of Textual."""

from collections.abc import Iterable

from regie.controllers.actions import ActionRecord, ActionState


def _signature(record: ActionRecord) -> tuple[object, ...]:
    return (
        record.action,
        record.target_id,
        record.state,
        record.phase,
        record.job_handle,
        record.detail,
        repr(record.result),
    )


class ActionPresentation:
    def __init__(self) -> None:
        self._signatures: dict[str, tuple[object, ...]] = {}
        self._reconciled: set[str] = set()
        self._reconciling: set[str] = set()

    @property
    def reconciled(self) -> frozenset[str]:
        return frozenset(self._reconciled)

    def reconciling(self, record: ActionRecord) -> bool:
        return record.idempotency_key in self._reconciling

    def retain(self, records: Iterable[ActionRecord]) -> None:
        keys = {record.idempotency_key for record in records}
        self._signatures = {key: value for key, value in self._signatures.items() if key in keys}
        self._reconciled.intersection_update(keys)

    def changed(self, record: ActionRecord) -> bool:
        return _signature(record) != self._signatures.get(record.idempotency_key)

    def presented(self, record: ActionRecord) -> bool:
        changed = self.changed(record)
        self._signatures[record.idempotency_key] = _signature(record)
        return changed

    def needs_reconciliation(self, record: ActionRecord) -> bool:
        return (
            record.state is ActionState.SUCCEEDED
            and record.action in {"spawn", "resume", "terminate"}
            and record.idempotency_key not in self._reconciled
            and not self.reconciling(record)
        )

    def begin_reconciliation(self, record: ActionRecord) -> bool:
        if not self.needs_reconciliation(record):
            return False
        self._reconciling.add(record.idempotency_key)
        return True

    def finish_reconciliation(self, record: ActionRecord, *, succeeded: bool) -> None:
        self._reconciling.discard(record.idempotency_key)
        if succeeded:
            self._reconciled.add(record.idempotency_key)
