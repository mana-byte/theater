"""Immutable operation and attribute specifications for observability."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from string import Formatter
from types import MappingProxyType
from typing import Any

from theater.constants.observability import DEFAULT_SLOW_MS, GIT_MS, PROC_MS, TMUX_MS, WORKERS_MS


class TraceKind(Enum):
    NONE = "none"
    INTERNAL = "internal"
    CLIENT = "client"
    SERVER = "server"


class ValueTransform(Enum):
    RAW = "raw"
    STRING = "string"
    WORKTREE_KIND = "worktree_kind"


type AttributeValue = bool | int | float | str


@dataclass(frozen=True, slots=True)
class AttrMapping:
    """Maps a caller keyword to prose/OTel-log/metric/trace attribute names.

    None means omit from that signal. result is implicit (engine-generated),
    never an explicit AttrMapping.
    """

    source: str
    prose_key: str | None = None
    otel_log_key: str | None = None
    metric_key: str | None = None
    trace_key: str | None = None
    prose_transform: ValueTransform = ValueTransform.RAW
    log_transform: ValueTransform = ValueTransform.RAW
    metric_transform: ValueTransform = ValueTransform.RAW
    trace_transform: ValueTransform = ValueTransform.RAW


@dataclass(frozen=True, slots=True)
class OperationSpec:
    key: str
    log_template: str | None
    trace_template: str | None
    metric_name: str | None
    description: str | None
    unit: str = "ms"
    slow_ms: float = DEFAULT_SLOW_MS
    trace_kind: TraceKind = TraceKind.INTERNAL
    record_outcome: bool = True
    static_attrs: tuple[tuple[str, AttributeValue], ...] = ()
    attrs: tuple[AttrMapping, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "static_attrs", tuple(self.static_attrs))
        object.__setattr__(self, "attrs", tuple(self.attrs))


def _worktree_kind(value: Any) -> str:
    if value is True:
        return "unique"
    if value is False or value is None:
        return "none"
    return "named"


def _apply_transform(value: Any, transform: ValueTransform) -> Any:
    if transform == ValueTransform.STRING:
        return str(value)
    if transform == ValueTransform.WORKTREE_KIND:
        return _worktree_kind(value)
    return value


def _extract_fields(template: str) -> set[str]:
    return {name for _, name, _, _ in Formatter().parse(template) if name}


def _validate_catalog(specs: tuple[OperationSpec, ...]) -> None:
    keys: set[str] = set()
    metric_specs: dict[str, OperationSpec] = {}
    for spec in specs:
        if spec.key in keys:
            raise ValueError(f"duplicate operation key: {spec.key}")
        keys.add(spec.key)
        if spec.metric_name is not None and spec.description is None:
            raise ValueError(f"metric {spec.metric_name}: missing description")
        if spec.metric_name is not None:
            existing = metric_specs.get(spec.metric_name)
            if existing is not None:
                if existing.description != spec.description:
                    raise ValueError(f"metric {spec.metric_name}: description mismatch")
                if existing.unit != spec.unit:
                    raise ValueError(f"metric {spec.metric_name}: unit mismatch")
                if existing.record_outcome != spec.record_outcome:
                    raise ValueError(f"metric {spec.metric_name}: record_outcome mismatch")
                existing_keys = {m.metric_key for m in existing.attrs if m.metric_key}
                existing_keys |= {k for k, _ in existing.static_attrs}
                new_keys = {m.metric_key for m in spec.attrs if m.metric_key}
                new_keys |= {k for k, _ in spec.static_attrs}
                if existing.record_outcome:
                    existing_keys.add("result")
                if spec.record_outcome:
                    new_keys.add("result")
                if existing_keys != new_keys:
                    raise ValueError(f"metric {spec.metric_name}: attribute key set mismatch")
            metric_specs[spec.metric_name] = spec
        _validate_templates(spec)


def _validate_templates(spec: OperationSpec) -> None:
    declared: set[str] = {m.source for m in spec.attrs}
    declared |= {k for k, _ in spec.static_attrs}
    for template in (spec.log_template, spec.trace_template):
        if template is None:
            continue
        for field_name in _extract_fields(template):
            if field_name not in declared:
                raise ValueError(f"{spec.key}: template field {field_name!r} not declared")


# --- Catalog entries (plan v9 section 6.1) ---

_PROC_ATTRS: tuple[AttrMapping, ...] = (
    AttrMapping(source="pid", prose_key="pid", otel_log_key="pid", trace_key="theater.pid"),
)

_CATALOG: tuple[OperationSpec, ...] = (
    OperationSpec(
        key="PROC_PS_TABLE",
        log_template="proc.ps-table",
        trace_template="proc.ps-table",
        metric_name="theater.process.command.duration",
        description="Duration of a process-info subprocess call.",
        slow_ms=PROC_MS,
        static_attrs=(("command", "ps-table"),),
        attrs=_PROC_ATTRS,
    ),
    OperationSpec(
        key="PROC_PS_COMM",
        log_template="proc.ps-comm",
        trace_template="proc.ps-comm",
        metric_name="theater.process.command.duration",
        description="Duration of a process-info subprocess call.",
        slow_ms=PROC_MS,
        static_attrs=(("command", "ps-comm"),),
        attrs=_PROC_ATTRS,
    ),
    OperationSpec(
        key="PROC_LSOF",
        log_template="proc.lsof",
        trace_template="proc.lsof",
        metric_name="theater.process.command.duration",
        description="Duration of a process-info subprocess call.",
        slow_ms=PROC_MS,
        static_attrs=(("command", "lsof"),),
        attrs=_PROC_ATTRS,
    ),
    OperationSpec(
        key="TMUX_COMMAND",
        log_template="tmux.{command}",
        trace_template="tmux.command {command}",
        metric_name="theater.tmux.command.duration",
        description="Duration of a tmux subprocess command.",
        slow_ms=TMUX_MS,
        attrs=(
            AttrMapping(
                source="command",
                otel_log_key="command",
                metric_key="command",
                trace_key="command",
            ),
        ),
    ),
    OperationSpec(
        key="GIT_COMMAND",
        log_template="git.{command}",
        trace_template="git.command {command}",
        metric_name="theater.git.command.duration",
        description="Duration of a git subprocess command.",
        slow_ms=GIT_MS,
        attrs=(
            AttrMapping(
                source="command",
                otel_log_key="command",
                metric_key="command",
                trace_key="command",
            ),
            AttrMapping(source="cwd", prose_key="cwd", otel_log_key="cwd", trace_key="theater.cwd"),
            AttrMapping(source="rc", prose_key="rc", otel_log_key="rc"),
        ),
    ),
    OperationSpec(
        key="WORKER_TASK",
        log_template="workers.{label}",
        trace_template="worker.task {label}",
        metric_name="theater.worker.task.duration",
        description="Duration of a worker task execution.",
        slow_ms=WORKERS_MS,
        attrs=(
            AttrMapping(
                source="label",
                otel_log_key="label",
                metric_key="task",
                trace_key="task",
            ),
        ),
    ),
    OperationSpec(
        key="SPAWN_WORKTREE",
        log_template="spawn.worktree",
        trace_template="spawn.worktree",
        metric_name="theater.spawn.worktree.duration",
        description="Duration of a worktree creation for spawn.",
        slow_ms=DEFAULT_SLOW_MS,
        attrs=(
            AttrMapping(source="id", prose_key="id", otel_log_key="id", trace_key="theater.id"),
            AttrMapping(
                source="kind",
                prose_key="kind",
                otel_log_key="worktree_kind",
                metric_key="kind",
                trace_key="theater.worktree.kind",
                metric_transform=ValueTransform.WORKTREE_KIND,
                trace_transform=ValueTransform.WORKTREE_KIND,
            ),
        ),
    ),
    OperationSpec(
        key="SPAWN_LAUNCH",
        log_template="spawn.launch",
        trace_template="spawn.launch",
        metric_name="theater.spawn.launch.duration",
        description="Duration of a harness launch for spawn.",
        slow_ms=DEFAULT_SLOW_MS,
        attrs=(
            AttrMapping(source="id", prose_key="id", otel_log_key="id", trace_key="theater.id"),
            AttrMapping(
                source="harness",
                prose_key="harness",
                otel_log_key="harness",
                metric_key="harness",
                trace_key="harness",
            ),
        ),
    ),
    OperationSpec(
        key="KILL_PANE",
        log_template="kill.pane",
        trace_template="kill.pane",
        metric_name="theater.kill.pane.duration",
        description="Duration of a pane kill operation.",
        slow_ms=DEFAULT_SLOW_MS,
        attrs=(
            AttrMapping(source="id", prose_key="id", otel_log_key="id", trace_key="theater.id"),
            AttrMapping(
                source="pane", prose_key="pane", otel_log_key="pane", trace_key="theater.pane"
            ),
            AttrMapping(
                source="harness",
                otel_log_key="harness",
                metric_key="harness",
                trace_key="harness",
            ),
            AttrMapping(source="attempts", prose_key="attempts", otel_log_key="attempts"),
        ),
    ),
    OperationSpec(
        key="KILL_TEARDOWN",
        log_template="kill.teardown",
        trace_template="kill.teardown",
        metric_name="theater.kill.teardown.duration",
        description="Duration of a teardown after kill.",
        slow_ms=DEFAULT_SLOW_MS,
        attrs=(
            AttrMapping(source="id", prose_key="id", otel_log_key="id", trace_key="theater.id"),
            AttrMapping(
                source="harness",
                otel_log_key="harness",
                metric_key="harness",
                trace_key="harness",
            ),
        ),
    ),
    OperationSpec(
        key="RPC_SERVER",
        log_template="rpc.{method}",
        trace_template="rpc.server {method}",
        metric_name="theater.rpc.duration",
        description="Duration of a daemon-side RPC handler.",
        slow_ms=DEFAULT_SLOW_MS,
        trace_kind=TraceKind.SERVER,
        attrs=(
            AttrMapping(
                source="method",
                otel_log_key="method",
                metric_key="method",
                trace_key="method",
            ),
            AttrMapping(
                source="caller",
                prose_key="caller",
                otel_log_key="caller",
                trace_key="theater.caller",
            ),
        ),
    ),
    OperationSpec(
        key="RPC_AWAIT",
        log_template="rpc.jobs.await",
        trace_template="rpc.await",
        metric_name="theater.rpc.await.duration",
        description="Duration of a jobs.await RPC call.",
        slow_ms=float("inf"),
        trace_kind=TraceKind.SERVER,
        attrs=(
            AttrMapping(
                source="caller",
                prose_key="caller",
                otel_log_key="caller",
                trace_key="theater.caller",
            ),
        ),
    ),
    OperationSpec(
        key="OBSERVER_ATTACH",
        log_template="observer.attach",
        trace_template="observer.attach",
        metric_name="theater.observer.readiness.duration",
        description="Duration of observer readiness milestone.",
        slow_ms=DEFAULT_SLOW_MS,
        trace_kind=TraceKind.NONE,
        record_outcome=False,
        static_attrs=(("milestone", "attach"),),
        attrs=(
            AttrMapping(source="id", prose_key="id", otel_log_key="id", trace_key="theater.id"),
            AttrMapping(
                source="harness",
                prose_key="harness",
                otel_log_key="harness",
                metric_key="harness",
                trace_key="harness",
            ),
        ),
    ),
    OperationSpec(
        key="OBSERVER_WATCH",
        log_template="observer.watch",
        trace_template="observer.watch",
        metric_name="theater.observer.readiness.duration",
        description="Duration of observer readiness milestone.",
        slow_ms=DEFAULT_SLOW_MS,
        trace_kind=TraceKind.NONE,
        record_outcome=False,
        static_attrs=(("milestone", "watch"),),
        attrs=(
            AttrMapping(source="id", prose_key="id", otel_log_key="id", trace_key="theater.id"),
            AttrMapping(
                source="harness",
                prose_key="harness",
                otel_log_key="harness",
                metric_key="harness",
                trace_key="harness",
            ),
        ),
    ),
    OperationSpec(
        key="EVENT_LOOP_LAG",
        log_template=None,
        trace_template=None,
        metric_name="theater.eventloop.lag",
        description="Event-loop wake-up lag measurement.",
        trace_kind=TraceKind.NONE,
        record_outcome=False,
    ),
    OperationSpec(
        key="RPC_CLIENT",
        log_template=None,
        trace_template="rpc.client {method}",
        metric_name=None,
        description=None,
        trace_kind=TraceKind.CLIENT,
        attrs=(AttrMapping(source="method", otel_log_key="method", trace_key="method"),),
    ),
)

_validate_catalog(_CATALOG)

OPERATIONS: tuple[OperationSpec, ...] = _CATALOG
BY_KEY: Mapping[str, OperationSpec] = MappingProxyType({spec.key: spec for spec in _CATALOG})
RESULTS: tuple[str, ...] = ("success", "error", "cancelled")
