"""Catalog: immutable specs, metric metadata, mappings."""

from __future__ import annotations

from functools import partial

import pytest

from tests.rig.tables import eq_row, is_row, run_rows
from theater.observability.catalog import (
    BY_KEY,
    OPERATIONS,
    RESULTS,
    AttrMapping,
    OperationSpec,
    TraceKind,
    ValueTransform,
    _worktree_kind,
)


def test_tuple_unique():
    keys = [s.key for s in OPERATIONS]
    assert len(keys) == len(set(keys)) == 23


def test_by_key_readonly():
    with pytest.raises(TypeError):
        BY_KEY["X"] = BY_KEY["PROC_PS_TABLE"]  # type: ignore[index]


def test_spec_copies_sequence_inputs():
    attrs = [AttrMapping("value", metric_key="value")]
    spec = OperationSpec("TEST", "test", "test", "test.duration", "test", attrs=attrs)
    attrs.clear()
    assert len(spec.attrs) == 1


def test_trace_kinds():
    """Every checked operation carries its documented trace kind."""
    run_rows(
        [
            eq_row("PROC_PS_TABLE", lambda: BY_KEY["PROC_PS_TABLE"].trace_kind, TraceKind.INTERNAL),
            eq_row("RPC_CLIENT", lambda: BY_KEY["RPC_CLIENT"].trace_kind, TraceKind.CLIENT),
            eq_row("RPC_SERVER", lambda: BY_KEY["RPC_SERVER"].trace_kind, TraceKind.SERVER),
            eq_row("RPC_AWAIT", lambda: BY_KEY["RPC_AWAIT"].trace_kind, TraceKind.SERVER),
            eq_row("OBSERVER_ATTACH", lambda: BY_KEY["OBSERVER_ATTACH"].trace_kind, TraceKind.NONE),
            eq_row("EVENT_LOOP_LAG", lambda: BY_KEY["EVENT_LOOP_LAG"].trace_kind, TraceKind.NONE),
        ]
    )


def test_record_outcome():
    """Only the proc fact records its outcome into the span."""
    run_rows(
        [
            is_row("PROC_PS_TABLE", lambda: BY_KEY["PROC_PS_TABLE"].record_outcome, True),
            is_row("OBSERVER_ATTACH", lambda: BY_KEY["OBSERVER_ATTACH"].record_outcome, False),
            is_row("EVENT_LOOP_LAG", lambda: BY_KEY["EVENT_LOOP_LAG"].record_outcome, False),
        ]
    )


def test_proc_pid_prose():
    """The proc facts report the pid through prose and trace, never a metric."""

    def check(key: str) -> None:
        m = next(m for m in BY_KEY[key].attrs if m.source == "pid")
        assert m.prose_key == "pid"
        assert m.trace_key == "theater.pid"
        assert m.metric_key is None

    run_rows((key, partial(check, key)) for key in ("PROC_PS_TABLE", "PROC_PS_COMM", "PROC_LSOF"))


def test_rpc_client_no_metric_no_log():
    s = BY_KEY["RPC_CLIENT"]
    assert s.metric_name is None and s.log_template is None and s.trace_template is not None


def test_event_loop_lag_no_log_no_trace():
    s = BY_KEY["EVENT_LOOP_LAG"]
    assert s.log_template is None and s.trace_template is None


def test_shared_metric_consistent():
    ps = [s for s in OPERATIONS if s.metric_name == "theater.process.command.duration"]
    assert len(ps) == 3
    assert all(s.description == ps[0].description for s in ps)


def test_no_explicit_result_in_attrs():
    for s in OPERATIONS:
        for m in s.attrs:
            assert m.metric_key != "result" and m.source != "result"


def test_kill_harness_not_prose():
    """Killing reports the harness as a metric tag, not prose."""

    def check(key: str) -> None:
        m = next(m for m in BY_KEY[key].attrs if m.source == "harness")
        assert m.prose_key is None and m.metric_key == "harness"

    run_rows((key, partial(check, key)) for key in ("KILL_PANE", "KILL_TEARDOWN"))


def test_git_cwd_in_prose():
    m = next(m for m in BY_KEY["GIT_COMMAND"].attrs if m.source == "cwd")
    assert m.prose_key == "cwd"
    assert m.log_transform == ValueTransform.STRING
    assert m.trace_transform == ValueTransform.STRING


def test_spawn_launch_harness_in_prose():
    m = next(m for m in BY_KEY["SPAWN_LAUNCH"].attrs if m.source == "harness")
    assert m.prose_key == "harness"


def test_spawn_worktree_kind_source():
    m = next(m for m in BY_KEY["SPAWN_WORKTREE"].attrs if m.source == "kind")
    assert m.prose_key == "kind" and m.metric_key == "kind"
    assert m.metric_transform == ValueTransform.WORKTREE_KIND


def test_worker_label_no_prose():
    m = next(m for m in BY_KEY["WORKER_TASK"].attrs if m.source == "label")
    assert m.metric_key == "task" and m.prose_key is None


def test_kill_pane_attempts_mapping():
    m = next(m for m in BY_KEY["KILL_PANE"].attrs if m.source == "attempts")
    assert m.prose_key == "attempts" and m.metric_key is None


def test_worktree_kind():
    assert _worktree_kind(True) == "unique"
    assert _worktree_kind(False) == "none"
    assert _worktree_kind(None) == "none"
    assert _worktree_kind("x") == "named"


def test_results():
    assert RESULTS == ("success", "error", "cancelled")


def test_rpc_await_inf():
    assert BY_KEY["RPC_AWAIT"].slow_ms == float("inf")
