"""The rescue rate, and the refusals that never became jobs.

The rescue path is the reason this file exists. When the observer never sees a
turn end, it waits out the rescue timer and finishes the job with whatever the
agent was last heard to say, tagged `turn_end_unseen`. The caller cannot tell
that apart from a real answer, so a harness whose transcript format has drifted
looks *slow*, not *broken*. Counting it is what turns that into a number
someone can act on.

The counts are derived from the jobs table rather than from live counters, so
these tests write jobs and assert on what comes back out — no observer, no
tmux, no time passing.
"""

from __future__ import annotations

import pytest

from theater.models import Job, JobState, Participant, Tier, now
from theater.protocol import RemoteError


def _turn(
    store,
    target: Participant,
    *,
    handle: str,
    state: str = JobState.DONE,
    error_code: str | None = None,
    created_at: float | None = None,
) -> None:
    """One prompt-carrying job against a participant."""
    store.create_job(
        _job(
            handle=handle,
            target_id=target.id,
            state=state,
            error_code=error_code,
            created_at=created_at,
        )
    )


def _job(
    *,
    handle: str,
    target_id: str | None,
    kind: str = "send",
    prompt: str | None = "do the thing",
    state: str = JobState.DONE,
    error_code: str | None = None,
    created_at: float | None = None,
) -> Job:
    """Job has no defaults on purpose — every field is load-bearing in the
    daemon — so the tests get their own constructor rather than ten of these."""
    return Job(
        handle=handle,
        caller_id="cli",
        target_id=target_id,
        kind=kind,
        prompt=prompt,
        state=state,
        result=None,
        error_code=error_code,
        created_at=created_at if created_at is not None else now(),
        finished_at=None if state == JobState.RUNNING else now(),
    )


def _participant(store, harness: str) -> Participant:
    p = Participant(harness=harness, tier=Tier.SPAWNED, tmux_pane="%1", cwd="/tmp")
    store.upsert_participant(p)
    return p


# ---- turn outcomes ------------------------------------------------------


def test_outcomes_separate_clean_from_rescued(store):
    vibe = _participant(store, "vibe")
    _turn(store, vibe, handle="a#1")
    _turn(store, vibe, handle="a#2", error_code="turn_end_unseen")
    _turn(store, vibe, handle="a#3", state=JobState.CRASHED, error_code="send_failed")
    _turn(store, vibe, handle="a#4", state=JobState.RUNNING)

    (row,) = store.turn_outcomes()
    assert row["harness"] == "vibe"
    assert (row["turns"], row["clean"], row["rescued"]) == (4, 1, 1)
    assert (row["failed"], row["running"]) == (1, 1)


def test_outcomes_are_per_harness(store):
    good = _participant(store, "vibe")
    drifting = _participant(store, "opencode")
    _turn(store, good, handle="a#1")
    _turn(store, drifting, handle="b#1", error_code="turn_end_unseen")
    _turn(store, drifting, handle="b#2", error_code="turn_end_unseen")

    by_harness = {r["harness"]: r for r in store.turn_outcomes()}
    assert by_harness["vibe"]["rescued"] == 0
    assert by_harness["opencode"]["rescued"] == 2
    # The whole point of splitting by harness: one drifting parser must not be
    # averaged away by three healthy ones.
    assert by_harness["opencode"]["clean"] == 0


def test_promptless_jobs_are_not_turns(store):
    """A job that asked for nothing is not evidence about turn detection."""
    vibe = _participant(store, "vibe")
    store.create_job(_job(handle="a#1", target_id=vibe.id, kind="spawn", prompt=None))
    assert store.turn_outcomes() == []


def test_a_forgotten_target_still_counts(store):
    """Under "unknown" rather than vanishing, which would flatter the numbers."""
    store.create_job(_job(handle="a#1", target_id="gone", error_code="turn_end_unseen"))
    (row,) = store.turn_outcomes()
    assert row["harness"] == "unknown"
    assert row["rescued"] == 1


def test_the_window_cuts_on_when_the_turn_was_asked(store):
    """Creation, not completion: a turn still running belongs to its own window."""
    vibe = _participant(store, "vibe")
    _turn(store, vibe, handle="old#1", created_at=now() - 7200)
    _turn(store, vibe, handle="new#1", created_at=now() - 60)

    assert store.turn_outcomes()[0]["turns"] == 2
    assert store.turn_outcomes(since=now() - 3600)[0]["turns"] == 1


# ---- refusals -----------------------------------------------------------


@pytest.mark.parametrize(
    "reason,setup",
    [
        ("human_present", "human"),
        ("busy", "busy"),
    ],
)
async def test_a_refused_send_is_recorded_on_the_bus(
    client, daemon, fake_tmux, monkeypatch, reason, setup
):
    """A refusal leaves no job, so without this it leaves no trace at all."""
    target = await client.call("hello", harness="vibe", pane="%1", cwd="/tmp")

    if setup == "human":
        import theater.daemon.methods as methods_mod

        async def human_here(pane_id):
            return True

        monkeypatch.setattr(methods_mod, "human_present", human_here)
    else:
        await client.call("send", target=target["id"], prompt="first")

    with pytest.raises(RemoteError):
        await client.call("send", target=target["id"], prompt="second")

    assert daemon.store.refusal_counts() == {reason: 1}
    refusals = [e for e in daemon.store.bus_tail() if e["kind"] == "send.refused"]
    assert refusals[-1]["to_id"] == target["id"]
    assert refusals[-1]["payload"]["reason"] == reason


async def test_an_unaddressable_target_is_recorded(client, daemon, fake_tmux):
    ext = await client.call("hello", harness="vibe", cwd="/tmp")
    with pytest.raises(RemoteError):
        await client.call("send", target=ext["id"], prompt="hi")
    assert daemon.store.refusal_counts() == {"not_addressable": 1}


async def test_a_delivered_send_records_no_refusal(client, daemon, fake_tmux):
    target = await client.call("hello", harness="vibe", pane="%1", cwd="/tmp")
    await client.call("send", target=target["id"], prompt="hi")
    assert daemon.store.refusal_counts() == {}


def test_refusals_respect_the_window(store):
    store.bus_append("send.refused", payload={"reason": "busy"})
    assert store.refusal_counts(since=now() - 60) == {"busy": 1}
    assert store.refusal_counts(since=now() + 60) == {}


# ---- the RPC ------------------------------------------------------------


async def test_stats_reports_the_rescue(client, daemon, fake_tmux):
    vibe = _participant(daemon.store, "vibe")
    _turn(daemon.store, vibe, handle="a#1")
    _turn(daemon.store, vibe, handle="a#2", error_code="turn_end_unseen")

    data = await client.call("stats")
    assert data["since"] is None
    (row,) = data["harnesses"]
    assert (row["clean"], row["rescued"]) == (1, 1)


async def test_stats_window_is_in_hours(client, daemon, fake_tmux):
    vibe = _participant(daemon.store, "vibe")
    _turn(daemon.store, vibe, handle="old#1", created_at=now() - 7200)
    _turn(daemon.store, vibe, handle="new#1")

    assert (await client.call("stats", window=1))["harnesses"][0]["turns"] == 1
    assert (await client.call("stats", window=3))["harnesses"][0]["turns"] == 2


# ---- rendering ----------------------------------------------------------


def _render(monkeypatch, capsys, payload: dict, *argv: str) -> str:
    from theater import cli

    monkeypatch.setattr(cli, "call_sync", lambda method, **kw: payload)
    assert cli.cmd_stats(cli._parser().parse_args(["stats", *argv])) == 0
    return capsys.readouterr().out


def _row(**over) -> dict:
    base = {
        "harness": "vibe",
        "turns": 0,
        "clean": 0,
        "rescued": 0,
        "failed": 0,
        "running": 0,
    }
    return {**base, **over}


def test_the_rate_is_against_finished_turns(monkeypatch, capsys):
    """A turn still running is not yet evidence either way.

    Counting it in the denominator would make a burst of activity read as an
    improvement — start ten turns and the rescue rate halves while nothing has
    actually got better.
    """
    out = _render(
        monkeypatch,
        capsys,
        {
            "harnesses": [
                _row(turns=12, clean=1, rescued=1, running=10),
            ],
            "refusals": {},
        },
    )
    assert "50%" in out


def test_a_harness_with_nothing_finished_has_no_rate(monkeypatch, capsys):
    """Rather than 0%, which would read as "healthy"."""
    out = _render(
        monkeypatch,
        capsys,
        {"harnesses": [_row(turns=3, running=3)], "refusals": {}},
    )
    assert "0%" not in out
    assert out.rstrip().endswith("-")


def test_refusals_are_listed_when_there_are_any(monkeypatch, capsys):
    out = _render(
        monkeypatch,
        capsys,
        {"harnesses": [_row(turns=1, clean=1)], "refusals": {"human_present": 3}},
    )
    assert "human_present 3" in out


def test_an_empty_database_says_so(monkeypatch, capsys):
    out = _render(monkeypatch, capsys, {"harnesses": [], "refusals": {}})
    assert "no turns recorded yet" in out
