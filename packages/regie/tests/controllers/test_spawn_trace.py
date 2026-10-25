"""The spawn pipeline's log lines: each step once, with the time it took and how late it arrived."""

from __future__ import annotations

import logging
import time
from dataclasses import replace
from types import MappingProxyType

import pytest
from regie.controllers.actions import ActionRecord, ActionState
from regie.controllers.spawn_trace import SpawnTrace

from packages.regie.tests.support.app_fakes import (
    make_operation,
    make_projection,
    with_operations,
)

START = 1_000.0  # monotonic
WALL = time.time()


@pytest.fixture(autouse=True)
def _capture(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="regie.latency")


def record(**kw) -> ActionRecord:
    return ActionRecord(
        "spawn",
        "spawn:target",
        "key-1",
        participant_id="participant-2",
        operation_id="op-1",
        state=ActionState.PENDING,
        submitted_at=START,
        **kw,
    )


def operation(phase: str, state: str = "running", *, created=None, updated=None):
    base = make_operation("spawn", state, phase, target="participant-2", updated_at=updated or WALL)
    return replace(base, created_at=created if created is not None else (updated or WALL) - 0.3)


def lines(caplog: pytest.LogCaptureFixture, prefix: str) -> list[str]:
    return [m for m in caplog.messages if m.startswith(prefix)]


def test_each_daemon_phase_is_logged_once_with_its_time_into_the_operation(caplog) -> None:
    trace = SpawnTrace()
    spawn = record()
    projection = with_operations(make_projection(), operation("launch_preparing", updated=WALL))

    for _ in range(3):  # a re-render with nothing new says nothing new
        trace.observe(projection, [spawn], now=START + 0.4, wall=WALL + 0.01)

    (line,) = lines(caplog, "spawn.phase")
    assert line.startswith("spawn.phase launch_preparing/running +400ms daemon_at=300ms late=10ms")
    assert "operation=op-1" in line

    later = with_operations(make_projection(), operation("workspace_ready", updated=WALL + 0.5))
    trace.observe(later, [spawn], now=START + 0.9, wall=WALL + 0.5)
    assert [m.split()[1] for m in lines(caplog, "spawn.phase")] == [
        "launch_preparing/running",
        "workspace_ready/running",
    ]


def test_a_phase_that_reached_the_screen_late_is_a_warning(caplog) -> None:
    trace = SpawnTrace()
    projection = with_operations(make_projection(), operation("terminal_bound", "succeeded"))
    trace.observe(projection, [record()], now=START + 2.0, wall=WALL + 1.5)

    (entry,) = [r for r in caplog.records if r.getMessage().startswith("spawn.phase")]
    assert entry.levelno == logging.WARNING and "late=1500ms" in entry.getMessage()


def test_the_human_s_enter_starts_the_clock_and_is_reported_once(caplog) -> None:
    trace = SpawnTrace()
    trace.entered(START - 0.25)
    projection = with_operations(make_projection(), operation("launch_reserved"))

    trace.observe(projection, [record()], now=START + 0.1, wall=WALL)
    trace.observe(projection, [record()], now=START + 0.2, wall=WALL)

    (gap,) = lines(caplog, "spawn.enter_to_submit")
    assert gap.startswith("spawn.enter_to_submit 250.0ms")
    assert lines(caplog, "spawn.phase")[0].split()[2] == "+350ms"  # counted from the Enter


def test_the_participant_is_reported_when_it_appears_gets_a_route_and_changes_status(
    caplog,
) -> None:
    trace = SpawnTrace()
    spawn = record()
    projection = make_projection()
    bare = replace(projection.participants["participant-2"], terminal_route=None, status="idle")
    routed = replace(bare, terminal_route=projection.participants["participant-2"].terminal_route)
    working = replace(routed, status="working")

    for participant, at in ((bare, 0.1), (bare, 0.2), (routed, 0.5), (working, 0.9)):
        shown = replace(
            projection,
            participants=MappingProxyType(
                {**projection.participants, "participant-2": participant}
            ),
        )
        trace.observe(shown, [spawn], now=START + at, wall=WALL)

    assert [m.split(" +")[0] for m in lines(caplog, "spawn.participant")] == [
        "spawn.participant routed=False status=idle",
        "spawn.participant routed=True status=idle",
        "spawn.participant routed=True status=working",
    ]


def test_visible_and_staged_report_the_whole_path_and_warn_when_it_was_long(caplog) -> None:
    trace = SpawnTrace()
    spawn = record()
    trace.observe(make_projection(), [spawn], now=START, wall=WALL)  # starts counting
    trace.visible(spawn, START + 0.33)
    trace.staged(spawn, START + 1.4)

    assert lines(caplog, "spawn.visible")[0].startswith("spawn.visible +330ms")
    staged = [r for r in caplog.records if r.getMessage().startswith("spawn.staged")]
    assert staged[0].levelno == logging.WARNING and "+1400ms" in staged[0].getMessage()


def test_other_actions_and_forgotten_spawns_leave_no_trace(caplog) -> None:
    trace = SpawnTrace()
    send = ActionRecord("send", "participant-2", "key-2", operation_id="op-9", submitted_at=START)
    projection = with_operations(make_projection(), operation("control_reserved"))
    trace.observe(projection, [send], now=START, wall=WALL)
    assert not lines(caplog, "spawn.")

    spawn = record()
    trace.observe(projection, [spawn], now=START, wall=WALL)
    trace.observe(projection, [], now=START + 1, wall=WALL)  # the record aged out
    assert not trace._origin and not trace._phase and not trace._participant
