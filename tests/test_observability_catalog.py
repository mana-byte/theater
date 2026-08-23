"""Catalog: immutable specs, metric metadata, mappings."""

from __future__ import annotations

import pytest

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
    assert len(keys) == len(set(keys)) == 16


def test_by_key_readonly():
    with pytest.raises(TypeError):
        BY_KEY["X"] = BY_KEY["PROC_PS_TABLE"]  # type: ignore[index]


def test_spec_copies_sequence_inputs():
    attrs = [AttrMapping("value", metric_key="value")]
    spec = OperationSpec("TEST", "test", "test", "test.duration", "test", attrs=attrs)
    attrs.clear()
    assert len(spec.attrs) == 1


@pytest.mark.parametrize(
    "key,kind",
    [
        ("PROC_PS_TABLE", TraceKind.INTERNAL),
        ("RPC_CLIENT", TraceKind.CLIENT),
        ("RPC_SERVER", TraceKind.SERVER),
        ("RPC_AWAIT", TraceKind.SERVER),
        ("OBSERVER_ATTACH", TraceKind.NONE),
        ("EVENT_LOOP_LAG", TraceKind.NONE),
    ],
)
def test_trace_kinds(key, kind):
    assert BY_KEY[key].trace_kind == kind


@pytest.mark.parametrize(
    "key,expected",
    [
        ("PROC_PS_TABLE", True),
        ("OBSERVER_ATTACH", False),
        ("EVENT_LOOP_LAG", False),
    ],
)
def test_record_outcome(key, expected):
    assert BY_KEY[key].record_outcome is expected


@pytest.mark.parametrize("key", ["PROC_PS_TABLE", "PROC_PS_COMM", "PROC_LSOF"])
def test_proc_pid_prose(key):
    m = next(m for m in BY_KEY[key].attrs if m.source == "pid")
    assert m.prose_key == "pid"
    assert m.trace_key == "theater.pid"
    assert m.metric_key is None


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


def test_observer_shared_description_generic():
    a, w = BY_KEY["OBSERVER_ATTACH"], BY_KEY["OBSERVER_WATCH"]
    assert a.description == w.description
    assert "attach" not in w.description.lower()


def test_no_explicit_result_in_attrs():
    for s in OPERATIONS:
        for m in s.attrs:
            assert m.metric_key != "result" and m.source != "result"


@pytest.mark.parametrize("key", ["KILL_PANE", "KILL_TEARDOWN"])
def test_kill_harness_not_prose(key):
    m = next(m for m in BY_KEY[key].attrs if m.source == "harness")
    assert m.prose_key is None and m.metric_key == "harness"


def test_git_cwd_in_prose():
    m = next(m for m in BY_KEY["GIT_COMMAND"].attrs if m.source == "cwd")
    assert m.prose_key == "cwd"


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
