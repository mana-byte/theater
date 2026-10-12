"""Values the play service decides with: a spawn's play facts, its seat, and refusals."""

from __future__ import annotations

from dataclasses import dataclass

from theater.models import BadRequest
from theater.skills import PlayRules


class PlayRefused(BadRequest):
    """A spawn or send breaks the rules of the live play it falls under."""

    code = "play_refused"


@dataclass(frozen=True, slots=True)
class PlayPlan:
    """The play facts of one request's attached skills, resolved when it is admitted.

    ``start`` holds the rules of an attached master skill, pinned if the spawn starts a play.
    """

    names: tuple[str, ...] = ()
    start: PlayRules | None = None

    def to_wire(self) -> dict[str, object]:
        return {
            "names": list(self.names),
            "start": None if self.start is None else self.start.to_json(),
        }

    @classmethod
    def from_wire(cls, value: object) -> PlayPlan:
        if not isinstance(value, dict):
            return cls()
        start = value.get("start")
        return cls(
            names=tuple(str(name) for name in value.get("names") or ()),
            start=PlayRules.from_json(start) if isinstance(start, str) else None,
        )


@dataclass(frozen=True, slots=True)
class SpawnSeat:
    """What a spawn makes of its child: nothing, a new master, or a position in a live play."""

    start: PlayRules | None = None
    master_id: str | None = None
    position: str | None = None


NO_SEAT = SpawnSeat()

__all__ = ["NO_SEAT", "PlayPlan", "PlayRefused", "SpawnSeat"]
