"""Facts staged by one Pi transcript attachment."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class StagedAttachment:
    usage_only_until: int | None
    dev: int | None
    ino: int | None
