"""Operation catalog specs and the observability config section."""

from __future__ import annotations

from functools import partial

import pytest

from tests.support.tables import eq_row, is_row, run_rows
from theater import config as cfg
from theater.config.models import ObservabilitySection
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
    assert len(keys) == len(set(keys)) == 33


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


def test_client_and_mcp_metrics_are_distinct_from_daemon_metrics():
    s = BY_KEY["RPC_CLIENT"]
    assert s.metric_name == "theater.rpc.client.duration"
    assert s.log_template is not None and s.trace_template is not None
    mcp = BY_KEY["MCP_TOOL"]
    assert mcp.metric_name == "theater.mcp.tool.duration"
    assert mcp.trace_kind is TraceKind.NONE and mcp.trace_template is None
    for spec in (s, mcp, BY_KEY["RPC_SERVER"]):
        assert all(m.metric_key not in {"call_id", "request_id"} for m in spec.attrs)


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


@pytest.mark.parametrize(
    ("operation", "source", "expected"),
    [
        (
            "GIT_COMMAND",
            "cwd",
            {
                "prose_key": "cwd",
                "log_transform": ValueTransform.STRING,
                "trace_transform": ValueTransform.STRING,
            },
        ),
        ("SPAWN_LAUNCH", "harness", {"prose_key": "harness"}),
        (
            "SPAWN_WORKTREE",
            "kind",
            {
                "prose_key": "kind",
                "metric_key": "kind",
                "metric_transform": ValueTransform.WORKTREE_KIND,
            },
        ),
        ("WORKER_TASK", "label", {"metric_key": "task", "prose_key": None}),
        ("KILL_PANE", "attempts", {"prose_key": "attempts", "metric_key": None}),
    ],
    ids=[
        "git-cwd",
        "spawn-launch-harness",
        "spawn-worktree-kind",
        "worker-label",
        "kill-pane-attempts",
    ],
)
def test_an_operation_attribute_is_mapped_to_its_sinks(operation, source, expected):
    mapping = next(m for m in BY_KEY[operation].attrs if m.source == source)
    assert {field: getattr(mapping, field) for field in expected} == expected


def test_worktree_kind():
    assert _worktree_kind(True) == "unique"
    assert _worktree_kind(False) == "none"
    assert _worktree_kind(None) == "none"
    assert _worktree_kind("x") == "named"


def test_results():
    assert RESULTS == ("success", "error", "cancelled")


def test_rpc_await_inf():
    assert BY_KEY["RPC_AWAIT"].slow_ms == float("inf")


def test_defaults():
    s = ObservabilitySection()
    assert s.otlp_enabled is False
    assert s.mcp_timing is False
    assert s.agent_metrics is True
    assert s.agent_logs is True
    assert s.agent_spans is True
    assert s.agent_log_content is False
    assert s.otlp_protocol == "grpc"
    assert s.otlp_endpoint is None
    assert s.service_name == "theater"
    assert s.export_interval_ms == 5000
    assert s.gauge_interval_s == 5.0
    assert s.log_max_bytes == 10_485_760
    assert s.log_backup_count == 3


def test_in_sections_and_config():
    assert "observability" in cfg._SECTIONS
    assert cfg._SECTIONS["observability"] is ObservabilitySection
    assert isinstance(cfg.Config().observability, ObservabilitySection)


def _load(tmp_path, monkeypatch, text):
    from theater import paths

    (tmp_path / "config.toml").write_text(text)
    monkeypatch.setattr(paths, "home", lambda: tmp_path)
    return cfg.load()


def test_validation(tmp_path, monkeypatch):
    """Every invalid observability body fails with its named reason."""

    def rejects(body: str, match: str) -> None:
        with pytest.raises(cfg.ConfigError, match=match):
            _load(tmp_path, monkeypatch, body)

    run_rows(
        (body.strip().splitlines()[-1], partial(rejects, body, match))
        for body, match in [
            ("[observability]\nbad_key = true\n", "unknown key"),
            ('[observability]\notlp_protocol = "udp"\n', "must be one of"),
            ('[observability]\notlp_endpoint = ""\n', "must not be blank"),
            ('[observability]\nservice_name = "  "\n', "must not be blank"),
            ("[observability]\nexport_interval_ms = 10\n", "must be >="),
            ("[observability]\ngauge_interval_s = 0.001\n", "must be >="),
            ("[observability]\nlog_max_bytes = 100\n", "must be >="),
            ("[observability]\nlog_backup_count = 0\n", "must be >="),
            ("[observability]\ngauge_interval_s = inf\n", "finite"),
        ]
    )


@pytest.mark.parametrize("proto", ["grpc", "http"])
def test_protocol(tmp_path, monkeypatch, proto):
    assert (
        _load(
            tmp_path, monkeypatch, f'[observability]\notlp_protocol = "{proto}"\n'
        ).observability.otlp_protocol
        == proto
    )


def test_otlp_enabled(tmp_path, monkeypatch):
    assert (
        _load(
            tmp_path, monkeypatch, "[observability]\notlp_enabled = true\n"
        ).observability.otlp_enabled
        is True
    )


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("mcp_timing", True),
        ("agent_metrics", False),
        ("agent_logs", False),
        ("agent_spans", False),
        ("agent_log_content", True),
    ],
)
def test_agent_telemetry_options(tmp_path, monkeypatch, key, value):
    config = _load(tmp_path, monkeypatch, f"[observability]\n{key} = {str(value).lower()}\n")
    assert getattr(config.observability, key) is value
    rows = {k: (v, s) for k, v, s in cfg.describe(config)}
    assert rows[f"observability.{key}"] == (str(value), "config.toml")


def test_describe():
    rows = {k: (v, s) for k, v, s in cfg.describe(cfg.Config())}
    assert rows["observability.otlp_enabled"] == ("False", "default")
    assert rows["observability.agent_metrics"] == ("True", "default")
    assert rows["observability.agent_logs"] == ("True", "default")
    assert rows["observability.agent_spans"] == ("True", "default")
    assert rows["observability.agent_log_content"] == ("False", "default")
    assert rows["observability.otlp_protocol"] == ("grpc", "default")
    assert rows["observability.otlp_endpoint"] == ("(unset)", "default")


def test_frozen():
    with pytest.raises((AttributeError, TypeError)):
        ObservabilitySection().otlp_enabled = True  # type: ignore[misc]
