"""Public RPC outcome classification without changing refusal propagation."""

from types import TracebackType
from typing import Literal

from theater import timing
from theater.daemon.frontend.validation import PublicRequestError
from theater.observability.catalog import RPC_SERVER

_STATE_READS = frozenset(
    {
        "frontend.state.snapshot",
        "frontend.state.page",
        "frontend.state.release",
        "frontend.state.follow",
    }
)
_RECOVERY_CODES = frozenset({"resnapshot_required", "snapshot_expired"})


class PublicRequestTiming:
    def __init__(self, method: str, *, caller: str, slow_ms: float | None = None) -> None:
        self._method = method
        self._span = timing.span(RPC_SERVER, method=method, caller=caller, slow_ms=slow_ms)
        self._fields: dict[str, object] = {}

    def __enter__(self) -> None:
        self._fields = self._span.__enter__()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        if (
            self._method in _STATE_READS
            and isinstance(exc, PublicRequestError)
            and exc.code in _RECOVERY_CODES
        ):
            self._fields["recovery"] = exc.code
            self._span.__exit__(None, None, None)
        else:
            self._span.__exit__(exc_type, exc, traceback)
        return False
