"""Tests for centralized latency instrumentation."""

from __future__ import annotations

import logging

import pytest

from theater import timing
from theater.models import now as wall_now

TIMING = "theater.timing"


def _messages(caplog, level: int) -> list[str]:
    return [r.message for r in caplog.records if r.levelno == level]


def test_fast_span_stays_out_of_the_default_log(caplog):
    caplog.set_level(logging.DEBUG, logger=TIMING)
    with timing.span("t.fast", slow_ms=10_000.0):
        pass
    assert _messages(caplog, logging.INFO) == []
    assert len(_messages(caplog, logging.DEBUG)) == 1


def test_slow_span_reaches_info(caplog):
    caplog.set_level(logging.DEBUG, logger=TIMING)
    with timing.span("t.slow", slow_ms=0.0):
        pass
    assert len(_messages(caplog, logging.INFO)) == 1
    assert _messages(caplog, logging.DEBUG) == []


def test_fast_span_is_silent_without_a_trace(caplog):
    caplog.set_level(logging.INFO, logger=TIMING)
    with timing.span("t.quiet", slow_ms=10_000.0):
        pass
    assert caplog.records == []


def test_span_logs_when_the_block_raises(caplog):
    caplog.set_level(logging.DEBUG, logger=TIMING)
    with pytest.raises(ValueError, match="boom"), timing.span("t.boom", slow_ms=0.0):
        raise ValueError("boom")
    assert len(_messages(caplog, logging.INFO)) == 1
    assert "t.boom" in _messages(caplog, logging.INFO)[0]


def test_fields_discovered_inside_the_block_are_logged(caplog):
    caplog.set_level(logging.DEBUG, logger=TIMING)
    with timing.span("t.retry", slow_ms=0.0, id="abc") as sp:
        sp["attempts"] = 4
    line = _messages(caplog, logging.INFO)[0]
    assert "id=abc" in line
    assert "attempts=4" in line


def test_fields_set_before_a_raise_survive_it(caplog):
    caplog.set_level(logging.DEBUG, logger=TIMING)
    with pytest.raises(RuntimeError), timing.span("t.partial", slow_ms=0.0) as sp:
        sp["attempts"] = 2
        raise RuntimeError
    assert "attempts=2" in _messages(caplog, logging.INFO)[0]


def test_none_fields_are_dropped():
    rendered = timing._render("t.x", 12.0, {"kept": "yes", "gone": None})
    assert rendered == "t.x 12.0ms kept=yes"


def test_duration_precedes_the_fields():
    rendered = timing._render("t.x", 1843.25, {"rc": 0})
    assert rendered == "t.x 1843.2ms rc=0"


def test_emit_renders_like_a_span(caplog):
    caplog.set_level(logging.DEBUG, logger=TIMING)
    timing.emit("t.gap", 2000.0, slow_ms=0.0, id="abc")
    assert _messages(caplog, logging.INFO) == ["t.gap 2000.0ms id=abc"]


def test_enable_trace_only_touches_this_logger(caplog):
    other = logging.getLogger("theater.observer")
    before = other.level
    try:
        timing.enable_trace()
        assert logging.getLogger(TIMING).level == logging.DEBUG
        assert other.level == before
    finally:
        logging.getLogger(TIMING).setLevel(logging.NOTSET)


def test_ready_lag_is_measured_from_creation(caplog):
    caplog.set_level(logging.DEBUG, logger=TIMING)
    timing.ready_lag("observer.attach", "abc", wall_now() - 2.0, harness="codex")
    line = _messages(caplog, logging.INFO)[0]
    assert line.startswith("observer.attach 2")
    assert "id=abc harness=codex" in line


def test_ready_lag_ignores_a_daemon_restart(caplog):
    caplog.set_level(logging.DEBUG, logger=TIMING)
    timing.ready_lag("observer.watch", "abc", wall_now() - 4_000.0)
    assert caplog.records == []


def test_ready_lag_ignores_a_clock_that_went_backwards(caplog):
    caplog.set_level(logging.DEBUG, logger=TIMING)
    timing.ready_lag("observer.watch", "abc", wall_now() + 30.0)
    assert caplog.records == []


def test_ready_lag_tolerates_a_participant_with_no_creation_time(caplog):
    caplog.set_level(logging.DEBUG, logger=TIMING)
    timing.ready_lag("observer.watch", "abc", None)
    assert caplog.records == []
