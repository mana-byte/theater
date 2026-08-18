"""The pre-flight gate: never type into a pane that is no longer the target's.

The failure under test is the only irreversible one Theater has. When a CLI
exits, its pane falls back to a shell, and a paste plus Enter runs the prompt
as a shell command — the `(eval):1: not enough directory stack entries` an
agent once received as its "reply". Everything else in the system produces a
wrong answer that can be retried.

What these tests can and cannot prove: they drive the real gate over a fake
tmux, so they prove the decision logic — which evidence refuses, which
evidence is not enough, and what happens to the participant record. They do
not prove that a real dead pane looks the way the fake says it does. That is
Phase D's rig, and until it exists these tests are a statement about intent
rather than about tmux.
"""

from __future__ import annotations

import pytest

from theater.protocol import RemoteError


async def _target(client, fake_tmux, daemon, *, pane="%1", command="vibe", pid=4242):
    """An addressable vibe participant sitting in a pane that really exists."""
    fake_tmux.add_pane(pane, command=command, pid=pid)
    target = await client.call("hello", harness="vibe", pane=pane, cwd="/tmp")
    participant = daemon.registry.get(target["id"])
    participant.session_id = f"session-{target['id']}"
    participant.session_correlation = "operator"
    daemon.store.upsert_participant(participant)
    return target


def _refusals(daemon):
    return [e for e in daemon.store.bus_tail(limit=100) if e["kind"] == "send.refused"]


# --- the pane is gone -------------------------------------------------------


async def test_send_into_a_closed_pane_is_refused(client, fake_tmux, daemon):
    """The CLI exited and took its window with it."""
    target = await _target(client, fake_tmux, daemon)
    fake_tmux.remove_pane("%1")

    with pytest.raises(RemoteError) as exc:
        await client.call("send", target=target["id"], prompt="are you there")

    assert exc.value.code == "stale_target"
    assert fake_tmux.sent == []


async def test_a_closed_pane_marks_the_participant_dead(client, fake_tmux, daemon):
    """tmux is the witness, so the conclusion is safe to act on."""
    target = await _target(client, fake_tmux, daemon)
    fake_tmux.remove_pane("%1")

    with pytest.raises(RemoteError):
        await client.call("send", target=target["id"], prompt="hi")

    assert daemon.registry.get(target["id"]).status.value == "dead"


async def test_a_closed_pane_records_its_reason_on_the_bus(client, fake_tmux, daemon):
    target = await _target(client, fake_tmux, daemon)
    fake_tmux.remove_pane("%1")

    with pytest.raises(RemoteError):
        await client.call("send", target=target["id"], prompt="hi")

    assert [r["payload"]["reason"] for r in _refusals(daemon)] == ["pane_gone"]


async def test_no_job_is_reserved_for_a_refused_send(client, fake_tmux, daemon):
    """The gate runs before the reservation, so nothing is left to clean up."""
    target = await _target(client, fake_tmux, daemon)
    fake_tmux.remove_pane("%1")

    with pytest.raises(RemoteError):
        await client.call("send", target=target["id"], prompt="hi")

    assert daemon.store.running_jobs_for_target(target["id"]) == []


# --- the pane was respawned under a new process -----------------------------


async def test_a_respawned_pane_is_refused(client, fake_tmux, daemon):
    """Same pane id, different process. tmux never recycles ids, but
    `respawn-pane` keeps one and replaces everything behind it."""
    target = await _target(client, fake_tmux, daemon, pid=4242)
    daemon.registry.attach_pane(target["id"], "%1", pane_pid=4242)
    fake_tmux.add_pane("%1", command="vibe", pid=9999)

    with pytest.raises(RemoteError) as exc:
        await client.call("send", target=target["id"], prompt="hi")

    assert exc.value.code == "stale_target"
    assert "4242" in str(exc.value) and "9999" in str(exc.value)
    assert fake_tmux.sent == []


async def test_a_respawned_pane_marks_the_participant_dead(client, fake_tmux, daemon):
    target = await _target(client, fake_tmux, daemon, pid=4242)
    daemon.registry.attach_pane(target["id"], "%1", pane_pid=4242)
    fake_tmux.add_pane("%1", command="vibe", pid=9999)

    with pytest.raises(RemoteError):
        await client.call("send", target=target["id"], prompt="hi")

    assert daemon.registry.get(target["id"]).status.value == "dead"
    assert [r["payload"]["reason"] for r in _refusals(daemon)] == ["pane_replaced"]


async def test_a_matching_epoch_delivers(client, fake_tmux, daemon):
    target = await _target(client, fake_tmux, daemon, pid=4242)
    daemon.registry.attach_pane(target["id"], "%1", pane_pid=4242)

    await client.call("send", target=target["id"], prompt="hi")

    assert fake_tmux.sent == [("%1", "hi")]


async def test_a_missing_epoch_skips_the_pid_check(client, fake_tmux, daemon):
    """`hello` records no epoch. That must not make a live agent unreachable:
    an unknown pid is an absence of evidence, not evidence of replacement."""
    target = await _target(client, fake_tmux, daemon, pid=4242)
    assert daemon.registry.get(target["id"]).pid is None

    await client.call("send", target=target["id"], prompt="hi")

    assert fake_tmux.sent == [("%1", "hi")]


# --- the harness exited and left a shell ------------------------------------


async def test_a_shell_at_the_prompt_is_refused(client, fake_tmux, daemon):
    """The bug this whole phase exists for."""
    target = await _target(client, fake_tmux, daemon, command="vibe")
    fake_tmux.add_pane("%1", command="zsh", pid=4242)

    with pytest.raises(RemoteError) as exc:
        await client.call("send", target=target["id"], prompt="ls -la /")

    assert exc.value.code == "stale_target"
    assert fake_tmux.sent == []
    assert [r["payload"]["reason"] for r in _refusals(daemon)] == ["harness_gone"]


async def test_a_dead_harness_does_not_mark_the_participant_dead(client, fake_tmux, daemon):
    """`ps` is the only witness here, and a `ps` that lied would cost a human
    an unexplained resurrection. Refuse, but leave the record alone."""
    target = await _target(client, fake_tmux, daemon, command="vibe")
    fake_tmux.add_pane("%1", command="zsh", pid=4242)

    with pytest.raises(RemoteError):
        await client.call("send", target=target["id"], prompt="hi")

    assert daemon.registry.get(target["id"]).status.value != "dead"


async def test_a_shell_with_the_harness_still_below_it_delivers(
    client, fake_tmux, daemon, monkeypatch
):
    """An agent running its own bash tool puts a shell in the foreground.

    That is the false positive the shell check would cause on its own, and
    the reason it is only ever read together with the process tree. Patched
    rather than staged, because the tree walk is a real `ps` call and the
    point here is the gate's logic, not the walk's.
    """
    import theater.daemon.harness_detect as harness_detect_mod

    target = await _target(client, fake_tmux, daemon, command="vibe")
    fake_tmux.add_pane("%1", command="zsh", pid=4242)
    # compare_pane_harness calls detect_harness from harness_detect, not methods.
    monkeypatch.setattr(harness_detect_mod, "detect_harness", lambda cmd, pid: "vibe")

    await client.call("send", target=target["id"], prompt="hi")

    assert fake_tmux.sent == [("%1", "hi")]


async def test_a_different_harness_in_the_seat_is_refused(client, fake_tmux, daemon):
    """The user quit vibe and started claude in the same pane."""
    target = await _target(client, fake_tmux, daemon, command="vibe")
    fake_tmux.add_pane("%1", command="claude", pid=4242)

    with pytest.raises(RemoteError) as exc:
        await client.call("send", target=target["id"], prompt="hi")

    assert exc.value.code == "stale_target"
    assert "claude" in str(exc.value)
    assert [r["payload"]["reason"] for r in _refusals(daemon)] == ["harness_changed"]


async def test_a_participant_of_unknown_harness_is_not_gated(client, fake_tmux):
    """Adopted panes whose harness could not be identified work today.

    There is nothing to compare against, so a second failed identification is
    not new evidence — refusing on it would be a regression dressed as a fix.
    """
    fake_tmux.add_pane("%9", command="zsh", pid=4242)
    target = await client.call("hello", harness="unknown", pane="%9", cwd="/tmp")

    await client.call("send", target=target["id"], prompt="hi")

    assert fake_tmux.sent == [("%9", "hi")]


# --- ordering and failure modes ---------------------------------------------


async def test_the_gate_runs_before_the_presence_check(client, fake_tmux, daemon, monkeypatch):
    """A pane that is not the target's is not worth scraping for a human."""
    import theater.daemon.methods as methods_mod

    target = await _target(client, fake_tmux, daemon)
    fake_tmux.remove_pane("%1")

    async def human_here(pane_id):
        raise AssertionError("presence was consulted for a pane that is gone")

    monkeypatch.setattr(methods_mod, "human_present", human_here)

    with pytest.raises(RemoteError) as exc:
        await client.call("send", target=target["id"], prompt="hi")

    assert exc.value.code == "stale_target"


async def test_the_gate_fails_open_when_tmux_errors(client, fake_tmux, daemon, monkeypatch):
    """A tmux hiccup must not become an unreachable participant.

    The gate exists to stop a delivery going somewhere wrong, not to become a
    second reason deliveries do not happen. If tmux is genuinely broken the
    paste right after fails on its own, and fails loudly.
    """
    from theater.tmux import client as tmux_client

    target = await _target(client, fake_tmux, daemon)

    async def broken(session=None):
        raise RuntimeError("tmux server gone")

    monkeypatch.setattr(tmux_client, "list_panes", broken)

    await client.call("send", target=target["id"], prompt="hi")

    assert fake_tmux.sent == [("%1", "hi")]
    assert _refusals(daemon) == []


async def test_the_gate_is_skipped_when_tmux_is_unavailable(client, fake_tmux, daemon, monkeypatch):
    from theater.tmux import client as tmux_client

    target = await _target(client, fake_tmux, daemon)
    monkeypatch.setattr(tmux_client, "available", lambda: False)

    await client.call("send", target=target["id"], prompt="hi")

    assert fake_tmux.sent == [("%1", "hi")]


# --- recording the launch epoch ---------------------------------------------


async def test_spawn_records_the_launch_epoch(client, fake_tmux, daemon):
    record = await client.call("spawn", harness="vibe", prompt="hi", approval="manual", cwd="/tmp")
    pane = next(p for p in fake_tmux.visible_panes if p.pane_id == record["tmux_pane"])

    assert daemon.registry.get(record["id"]).pid == pane.pane_pid


async def test_spawn_survives_an_unreadable_epoch(client, fake_tmux, monkeypatch):
    """The window exists and the harness is starting. Losing the agent over a
    bookkeeping lookup would be a far worse trade than losing one check."""
    from theater.tmux import client as tmux_client

    async def broken(session=None):
        raise RuntimeError("tmux server busy")

    monkeypatch.setattr(tmux_client, "list_panes", broken)

    record = await client.call("spawn", harness="vibe", prompt="hi", approval="manual", cwd="/tmp")

    assert record["tmux_pane"] is not None
    assert record["pid"] is None


async def test_adopt_records_the_launch_epoch(client, fake_tmux, daemon):
    """For an adopted pane the epoch is the shell tmux forked, not the
    harness — which is exactly why the pid check cannot stand alone."""
    fake_tmux.add_pane("%7", command="zsh", pid=777)

    record = await client.call("adopt", pane="%7")

    assert daemon.registry.get(record["id"]).pid == 777


async def test_re_attaching_the_same_pane_announces_nothing(client, fake_tmux, daemon):
    """`adopt` now re-attaches in order to record the epoch.

    That is bookkeeping, not news. Announcing it would put a move on the bus
    for a participant that never moved, and a bus full of phantom events is a
    bus nobody reads.
    """
    fake_tmux.add_pane("%7", command="zsh", pid=777)
    await client.call("adopt", pane="%7")

    moves = [e for e in daemon.store.bus_tail(limit=100) if e["kind"] == "participant.pane"]

    assert moves == []


async def test_a_real_move_is_still_announced(registry, store):
    """The other half of the same guard: silence is for unchanged panes only."""
    p = registry.create_spawned(harness="vibe", cwd="/tmp")
    registry.attach_pane(p.id, "%5", pane_pid=555)

    moves = [e for e in store.bus_tail(limit=100) if e["kind"] == "participant.pane"]

    assert [e["payload"]["pane"] for e in moves] == ["%5"]
    assert registry.get(p.id).pid == 555


# ---- copy mode: the one presence signal that is a tmux fact ---------------


async def _in_mode(monkeypatch, answer):
    """`human_present` over a tmux that reports `answer` for pane_in_mode."""
    from theater.tmux import presence

    async def fake_run(*args, **kw):
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(presence, "run", fake_run)
    return await presence.human_present("%1")


async def test_copy_mode_counts_as_a_human_at_the_pane(monkeypatch):
    """Injecting here would wipe the selection the user is in the middle of."""
    assert await _in_mode(monkeypatch, "1") is True


async def test_a_pane_in_no_mode_is_free_to_receive(monkeypatch):
    assert await _in_mode(monkeypatch, "0") is False


async def test_an_empty_answer_is_not_read_as_presence(monkeypatch):
    """tmux prints nothing for a pane it cannot format; that is not a human."""
    assert await _in_mode(monkeypatch, "") is False


async def test_a_tmux_that_cannot_be_asked_does_not_block_forever(monkeypatch):
    """Refusing on error would strand every send behind a transient tmux failure.

    The safe direction here is the opposite of the pane-liveness gate: this
    check only decides whether to queue, and the liveness gate still runs.
    """
    assert await _in_mode(monkeypatch, RuntimeError("no server")) is False
