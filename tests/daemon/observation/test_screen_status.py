"""Screen readings, awaiting-input hints and the screen-check cadence."""

from __future__ import annotations

import asyncio
import time
from itertools import pairwise
from types import SimpleNamespace

import pytest
from sqlalchemy import update

from tests.support.observation import LIVE_CHANNEL, apply_batch
from tests.support.observer_rigs import WaitingHarness, WaitingSource, screen_checked
from tests.support.shipped import VibeHarness
from tests.support.waiting import until
from theater.constants.observation import (
    AWAITING_INPUT_TARGET_SECONDS,
    LAST_ACTIVITY_REFRESH_SECONDS,
    QUIET_POLL_AFTER_SECONDS,
    QUIET_POLL_MAX_SECONDS,
    SCREEN_CHECK_BACKOFF_CAP_SECONDS,
    WORKING_SCREEN_INTERVAL_CAP_SECONDS,
)
from theater.daemon.observation.live import LiveRegistration
from theater.daemon.observer import (
    AWAITING_INPUT_TIMEOUT,
    Observer,
    QuietClock,
    TurnAccumulator,
)
from theater.daemon.schema import participants as participants_table
from theater.harness.base import Event, EventKind
from theater.harness.contracts.runtime import LiveChannelDeclaration
from theater.harness.observation import (
    ScreenConfidence,
    ScreenKind,
    ScreenReading,
)
from theater.harness.source import Batch
from theater.models import (
    Status,
)


def test_explicit_awaiting_input_status_survives_semantic_progress(registry):
    participant = registry.register(harness="codex", pane=None, cwd="/tmp")
    observer = Observer(registry, harnesses={})
    batch = Batch(status=Status.AWAITING_INPUT, progressed=True)

    assert apply_batch(
        observer,
        participant.id,
        batch,
        QuietClock(),
        TurnAccumulator(),
    )
    observer._unblock_on_semantic_progress(participant.id, batch)

    assert registry.get(participant.id).status is Status.AWAITING_INPUT


def test_unstatused_semantic_progress_clears_a_stale_awaiting_input_hint(registry):
    participant = registry.register(harness="codex", pane=None, cwd="/tmp")
    registry.set_status(participant.id, Status.AWAITING_INPUT)
    observer = Observer(registry, harnesses={})

    observer._unblock_on_semantic_progress(
        participant.id,
        Batch(events=[Event(kind=EventKind.ASSISTANT, text="continuing")]),
    )

    assert registry.get(participant.id).status is Status.WORKING


# The transcript only settles IDLE or WORKING: an approval modal is silence there,
# so the screen check must run for every non-DEAD status, not just WORKING.
@pytest.mark.parametrize(
    ("kind", "start", "expected"),
    [
        # A finished turn settles IDLE, so AWAITING_INPUT was unreachable under the old gate.
        pytest.param(
            ScreenKind.APPROVAL, Status.IDLE, Status.AWAITING_INPUT, id="approval-while-idle"
        ),
        # A turn that ended without an observed boundary must settle IDLE: `_rescue_jobs`
        # never touches participant status, so it would read WORKING forever.
        pytest.param(ScreenKind.PROMPT, Status.WORKING, Status.IDLE, id="prompt-while-working"),
    ],
)
async def test_the_screen_reading_settles_the_participant_status(registry, kind, start, expected):
    observer, screen, p = screen_checked(
        registry, reading=ScreenReading(kind, ScreenConfidence.LOW), status=start
    )
    await observer._check_idle_screen(p.id, screen)
    assert registry.get(p.id).status is expected


@pytest.mark.asyncio
async def test_an_unknown_screen_reading_leaves_the_status_untouched(registry):
    """UNKNOWN says nothing the reducer can act on."""
    observer, screen, p = screen_checked(
        registry,
        reading=ScreenReading(ScreenKind.UNKNOWN, ScreenConfidence.LOW),
        status=Status.WORKING,
    )
    await observer._check_idle_screen(p.id, screen)
    assert registry.get(p.id).status is Status.WORKING

    # And from IDLE, it stays IDLE.
    observer2, screen2, p2 = screen_checked(
        registry,
        reading=ScreenReading(ScreenKind.UNKNOWN, ScreenConfidence.LOW),
        status=Status.IDLE,
    )
    await observer2._check_idle_screen(p2.id, screen2)
    assert registry.get(p2.id).status is Status.IDLE


@pytest.mark.asyncio
async def test_a_native_participant_is_never_demoted_by_its_prompt_screen(registry):
    """A pane can show an idle composer mid-turn; the live channel owns status, not the screen."""
    observer, screen, p = screen_checked(
        registry,
        reading=ScreenReading(ScreenKind.PROMPT, ScreenConfidence.HIGH),
        status=Status.WORKING,
        awaiting=0.0,
    )
    observer.live.register(
        LiveRegistration(
            participant_id=p.id,
            live_source=WaitingSource(),
            channel=LiveChannelDeclaration(channel=LIVE_CHANNEL),
            backend_generation=1,
        )
    )
    await observer._screen_status_due(p.id, screen, QuietClock(screen_quiet_since=-1.0))
    assert registry.get(p.id).status is Status.WORKING

    observer.live.unregister(p.id)
    await observer._screen_status_due(p.id, screen, QuietClock(screen_quiet_since=-1.0))
    assert registry.get(p.id).status is Status.IDLE


@pytest.mark.asyncio
async def test_a_dead_participant_is_never_screen_checked(registry):
    """DEAD is terminal; a capture-pane on a dead pane is wasted work."""
    observer, screen, p = screen_checked(
        registry,
        reading=ScreenReading(ScreenKind.APPROVAL, ScreenConfidence.HIGH),
        status=Status.DEAD,
    )
    await observer._check_idle_screen(p.id, screen)
    assert registry.get(p.id).status is Status.DEAD


# A source still `waiting` for its input must keep the screen check alive: it is the
# only status channel that needs no transcript (Claude writes none before its first message).
@pytest.mark.asyncio
async def test_a_source_that_never_attaches_still_gets_a_screen_check(registry):
    """The regression, at the loop rather than the arm.

    Nothing is ever read, so only the waiting path runs. The pane shows an
    approval, and the participant must reach AWAITING_INPUT anyway.
    """
    harness = WaitingHarness(ScreenReading(ScreenKind.APPROVAL, ScreenConfidence.HIGH))
    observer = Observer(
        registry, {"waiting": harness}, poll=0.01, search=0.01, sync=0.01, awaiting=0.0
    )

    async def capture_pane(_pane):
        return "Do you want to proceed?"

    observer._capture = capture_pane
    observer.start()
    try:
        p = registry.register(harness="waiting", pane=None, cwd="/tmp")
        assert await until(lambda: registry.get(p.id).status is Status.AWAITING_INPUT)
    finally:
        await observer.aclose()


@pytest.mark.asyncio
async def test_quarantined_full_watch_moves_screen_without_reading_source(registry):
    """Restart replay quarantines before the source can attach or read."""
    harness = WaitingHarness(ScreenReading(ScreenKind.WORKING, ScreenConfidence.HIGH))
    participant = registry.register(harness="waiting", pane=None, cwd="/tmp")
    first = Observer(registry, harnesses={})
    first.mark_transcript_identity_lost(participant.id, "positive rotation evidence")
    observer = Observer(
        registry,
        {"waiting": harness},
        poll=0.01,
        search=0.01,
        awaiting=0.0,
    )

    async def capture_pane(_pane):
        return "rendered pane"

    observer._capture = capture_pane
    observer._tasks[participant.id] = asyncio.create_task(
        observer._watch(participant.id, "waiting")
    )
    try:
        assert await until(lambda: registry.get(participant.id).status is Status.WORKING)
        harness.observer._reading = ScreenReading(ScreenKind.PROMPT, ScreenConfidence.HIGH)
        assert await until(lambda: registry.get(participant.id).status is Status.IDLE)
        assert harness.source.reads == 0
        assert participant.id not in observer._bound_transcripts.values()
    finally:
        await observer.aclose()


@pytest.mark.asyncio
async def test_healthy_polling_replays_and_probes_pin_only_once(registry, vibe_tree, monkeypatch):
    from theater.harness.transcript import source as source_mod

    harness = VibeHarness(root=vibe_tree["root"])
    participant = registry.register(
        harness="vibe",
        pane=None,
        cwd=str(vibe_tree["project"]),
        session_id="deadbeef-1111-2222-3333",
    )
    participant.session_correlation = "exact"
    participant.transcript_location = str(vibe_tree["transcript"])
    registry.store.upsert_participant(participant)

    replay_calls = 0
    real_replay = registry.store.observation_error_active

    def replay_once(pid, code):
        nonlocal replay_calls
        replay_calls += 1
        return real_replay(pid, code)

    probe_calls = 0
    real_probe = source_mod.trusted_location_unavailable_reason

    def probe_once(**kwargs):
        nonlocal probe_calls
        probe_calls += 1
        return real_probe(**kwargs)

    monkeypatch.setattr(registry.store, "observation_error_active", replay_once)
    monkeypatch.setattr(source_mod, "trusted_location_unavailable_reason", probe_once)
    observer = Observer(
        registry,
        {"vibe": harness},
        poll=0.005,
        search=0.005,
        relocate=999.0,
    )
    observer.start()
    try:
        assert await until(lambda: participant.id in observer._bound_transcripts.values())
        await asyncio.sleep(0.05)
        assert replay_calls == 1
        assert probe_calls == 1
    finally:
        await observer.aclose()


@pytest.mark.asyncio
async def test_the_waiting_path_starts_neither_the_relocate_nor_the_rescue_clock(
    registry,
):
    """Only the screen arm runs there, and the other two must stay unstarted.

    A rescue over a source that has never read anything would finish a caller's
    job with an empty `last_text` — a wrong answer where a wait belongs.
    """
    observer, screen, p = screen_checked(
        registry,
        reading=ScreenReading(ScreenKind.APPROVAL, ScreenConfidence.HIGH),
    )
    observer.awaiting = 0.0
    clock = QuietClock()

    await observer._screen_only(p.id, screen, clock)
    assert clock.screen_quiet_since is not None
    assert clock.quiet_since is None
    assert clock.rescue_since is None


@pytest.mark.asyncio
async def test_the_waiting_screen_check_honours_its_own_window(registry):
    """No check until a full `awaiting` window of not-attaching has passed."""
    observer, screen, p = screen_checked(
        registry,
        reading=ScreenReading(ScreenKind.APPROVAL, ScreenConfidence.HIGH),
    )
    observer.awaiting = AWAITING_INPUT_TIMEOUT
    clock = QuietClock()

    # First pass only starts the clock.
    await observer._screen_only(p.id, screen, clock)
    assert registry.get(p.id).status is Status.IDLE

    # A window later, it fires.
    clock.screen_quiet_since = time.monotonic() - AWAITING_INPUT_TIMEOUT - 1
    await observer._screen_only(p.id, screen, clock)
    assert registry.get(p.id).status is Status.AWAITING_INPUT


def test_restating_a_status_refreshes_last_activity_only_every_few_seconds(registry, monkeypatch):
    """Every refresh is a commit, so an unchanged status must not stamp on every call."""
    p = registry.register(harness="vibe", pane=None, cwd="/tmp")
    observer = Observer(registry, harnesses={})
    registry.set_status(p.id, Status.WORKING)
    touches: list[str] = []
    monkeypatch.setattr(registry, "touch", touches.append)

    observer._settle(p.id, Status.WORKING)
    assert touches == []  # stamped by set_status moments ago

    stale = registry.store.get_participant(p.id)
    registry.store.conn.execute(
        update(participants_table)
        .where(participants_table.c.id == p.id)
        .values(last_activity=stale.last_activity - LAST_ACTIVITY_REFRESH_SECONDS - 1)
    )
    observer._settle(p.id, Status.WORKING)
    assert touches == [p.id]


@pytest.mark.asyncio
async def test_an_unchanged_prompt_screen_is_inspected_at_growing_intervals_until_stirred(registry):
    """A quiet prompt's screen arm backs off to its cap, and any progress restores the base."""
    now = [0.0]
    observer, screen, p = screen_checked(
        registry,
        reading=ScreenReading(ScreenKind.PROMPT, ScreenConfidence.LOW),
        awaiting=1.0,
        monotonic_clock=lambda: now[0],
    )
    inspects: list[float] = []

    async def capture_pane(_pane):
        inspects.append(now[0])
        return "$ "

    observer._capture = capture_pane
    clock = QuietClock()
    for tick in range(0, 40):
        now[0] = float(tick)
        await observer._screen_status_due(p.id, screen, clock)

    gaps = [b - a for a, b in pairwise(inspects)]
    assert gaps[0] < gaps[-1] <= SCREEN_CHECK_BACKOFF_CAP_SECONDS + 1
    assert len(inspects) < 15

    clock.stir()
    before = len(inspects)
    await observer._screen_status_due(p.id, screen, clock)
    now[0] += 1.5
    await observer._screen_status_due(p.id, screen, clock)
    assert len(inspects) == before + 1  # progress restored the base period


@pytest.mark.asyncio
async def test_an_approval_after_a_working_screen_is_seen_within_target_including_capture(registry):
    """Slowest production cadence (1.0s tick), a timed capture, and no transcript event."""
    now = [0.0]
    observer, screen, p = screen_checked(
        registry,
        reading=ScreenReading(ScreenKind.WORKING, ScreenConfidence.LOW),
        status=Status.WORKING,
        awaiting=1.5,
        monotonic_clock=lambda: now[0],
    )
    reads: list[float] = []

    async def capture_pane(_pane):
        reads.append(now[0])
        now[0] += 0.25  # the capture takes time; the clock keeps running through the await
        return "working"

    observer._capture = capture_pane
    clock = QuietClock()
    tick = QUIET_POLL_MAX_SECONDS
    for _ in range(20):
        now[0] += tick
        await observer._screen_status_due(p.id, screen, clock)
    assert clock.screen_interval(1.5) <= WORKING_SCREEN_INTERVAL_CAP_SECONDS

    # Flip at the instant of the last capture read: the worst moment for the next check.
    screen._reading = ScreenReading(ScreenKind.APPROVAL, ScreenConfidence.LOW)
    flipped = reads[-1]
    while registry.get(p.id).status is not Status.AWAITING_INPUT:
        now[0] += tick
        await observer._screen_status_due(p.id, screen, clock)
        assert now[0] - flipped <= AWAITING_INPUT_TARGET_SECONDS


def test_a_watcher_with_no_progress_for_a_while_polls_slower_and_progress_restores_it(registry):
    now = [100.0]
    observer = Observer(registry, harnesses={}, poll=0.25, monotonic_clock=lambda: now[0])
    context = SimpleNamespace(active_at=100.0)

    assert observer._quiet_poll(context, 0.25) == 0.25  # recently active
    now[0] += QUIET_POLL_AFTER_SECONDS + 1
    assert observer._quiet_poll(context, 0.25) == 1.0  # quiet: stretched, capped
    assert observer._quiet_poll(context, 0) == 0  # a batch with more to read is never delayed
    context.active_at = now[0]
    assert observer._quiet_poll(context, 0.25) == 0.25  # new output restores the fast poll
