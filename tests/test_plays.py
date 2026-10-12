"""Plays: a master skill starts one, positions seat every spawn, a send assigns on delivery."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from tests._presence_doubles import AbsentPresence
from tests.test_control_rpc import _install_runtime, _native_pair
from tests.test_handoff_send import _pump, _source
from theater.daemon.frontend.catalog_handlers import skills_load as frontend_skills_load
from theater.daemon.gc import _delete_participant_dependents
from theater.daemon.plays import PlayPlan, PlayRefused
from theater.daemon.plugins.dispatch import _skills_load as plugin_skills_load
from theater.daemon.rpc.cancellation import _cancel
from theater.daemon.schema import play_instances, play_members
from theater.models import JobState
from theater.protocol import RemoteError
from theater.skills import SkillValidationError, UnknownSkill, discover
from theater.skills.plays import parse_play_manifest

MASTER = "teams/master-lead"
LEAD = "teams/team-lead"
MEMBER = "teams/team-member"

SQUAD = """
description = "A squad."
master = "boss"

[positions]
boss = ["lead"]
lead = []

[roles]
scout = ["lead"]
"""


@pytest.fixture
async def absent(daemon):
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(daemon, "presence", AbsentPresence(), raising=False)
        yield


def _write_play(root, name: str, members: tuple[str, ...]) -> None:
    (root / name).mkdir(parents=True)
    (root / name / "PLAY.toml").write_text(SQUAD)
    for member in members:
        (root / name / member).mkdir()
        (root / name / member / "SKILL.md").write_text(
            f"---\nname: {member}\ndescription: The {member}.\n---\n\n# {member}\n"
        )


async def _spawn(client, parent: dict | None, *skills: str, **extra) -> dict:
    return await client.call(
        "spawn",
        harness="vibe",
        approval="manual",
        cwd="/tmp",
        prompt="work",
        parent_id=None if parent is None else parent["id"],
        skills=list(skills),
        **extra,
    )


async def _load(client, caller_id: str | None = None) -> dict | None:
    params = {"name": MASTER} | ({"caller_id": caller_id} if caller_id else {})
    return (await client.call("skills.load", **params)).get("play_start")


def _seat(daemon, participant_id: str, master_id: str, position: str) -> None:
    with daemon.store.write_unit() as unit:
        daemon.store.plays.add_member(
            participant_id, master_id, position, created_at=0.0, connection=unit.connection
        )


@pytest.mark.parametrize(
    ("manifest", "error"),
    [
        (SQUAD.replace('boss = ["lead"]', 'boss = ["lead", "boss"]'), "cannot spawn the master"),
        (SQUAD.replace("lead = []", "lead = []\nlost = []"), "unreachable from the master"),
        (SQUAD.replace('scout = ["lead"]', 'scout = ["boss"]'), "fits unknown or master"),
        ('extra = "x"\n' + SQUAD, "must contain description, master, positions"),
    ],
)
def test_a_play_manifest_is_validated_as_data(manifest, error):
    with pytest.raises(SkillValidationError, match=error):
        parse_play_manifest(manifest, play="squad")


def test_play_members_load_by_qualified_name_and_the_bare_play_is_no_skill(tmp_path):
    _write_play(tmp_path, "squad", ("boss", "lead", "scout"))
    _write_play(tmp_path, "broken", ("boss", "lead"))

    registry = discover(user_dir=tmp_path)

    squad = registry.play("squad")
    assert squad is not None and squad.master_skill == "squad/boss"
    assert dict(squad.rules.positions) == {"boss": ("lead",), "lead": ()}
    assert registry.load("squad/scout").play == "squad"
    with pytest.raises(UnknownSkill):
        registry.load("squad")
    [rejection] = registry.rejections
    assert rejection.name == "broken" and "missing: scout" in rejection.error


async def test_loading_the_master_skill_starts_one_play_for_that_agent_only(client, daemon):
    master = daemon.registry.create_spawned(harness="vibe", cwd="/tmp")
    child = daemon.registry.create_spawned(harness="vibe", cwd="/tmp", parent_id=master.id)

    assert await _load(client) is None, "an operator read starts nothing"
    frontend = await frontend_skills_load(daemon, None, {"name": MASTER})
    sidecar = await plugin_skills_load(daemon, {"name": MASTER, "caller_id": master.id}, "p")
    assert "play_start" not in frontend and "play_start" not in sidecar
    assert daemon.plays.membership(master.id) is None
    assert (await _load(client, master.id))["state"] == "started"
    assert (await _load(client, master.id))["state"] == "already_master"
    refused = await _load(client, child.id)
    assert refused["state"] == "not_started" and "live teams play" in refused["reason"]
    row = await client.call("participants.get", id=master.id)
    assert row["play"] == {"play": "teams", "position": MASTER, "master": master.id}
    assert "play" not in await client.call("participants.get", id=child.id)


async def test_a_play_cannot_start_above_a_live_master(client, daemon):
    parent = daemon.registry.create_spawned(harness="vibe", cwd="/tmp")
    child = daemon.registry.create_spawned(harness="vibe", cwd="/tmp", parent_id=parent.id)
    assert (await _load(client, child.id))["state"] == "started"

    refused = await _load(client, parent.id)

    assert refused["state"] == "not_started"
    assert f"{child.id} below you holds {MASTER}" in refused["reason"]
    assert daemon.plays.membership(parent.id) is None
    daemon.registry.mark_dead(child.id)
    assert (await _load(client, parent.id))["state"] == "started", "an ended play no longer blocks"


async def test_spawns_in_a_live_play_attach_one_position_the_spawner_may_give(
    client, daemon, terminal_provider
):
    for skills, needle in [
        ((LEAD,), f"the operator is in no live play, so no play can seat {LEAD}. Start one"),
        ((MASTER, "teams/worker"), f"a new master holds only {MASTER}"),
    ]:
        with pytest.raises(RemoteError, match=needle):
            await _spawn(client, None, *skills)
    master = await _spawn(client, None, MASTER)
    assert daemon.plays.membership(master["id"]).is_master, "a spawn can start a child master"
    for skills, needle in [
        ((), "attaches exactly one position: teams/team-lead; got none"),
        ((MEMBER,), "got teams/team-member"),
        ((LEAD, "teams/worker", "teams/reviewer"), "at most one task role"),
        ((MASTER,), "no play can start inside a live play"),
    ]:
        with pytest.raises(RemoteError) as refused:
            await _spawn(client, master, *skills)
        assert refused.value.code == "play_refused", skills
        assert needle in refused.value.message, refused.value.message

    lead = await _spawn(client, master, LEAD, "teams/worker", "theater-debate")
    member = await _spawn(client, lead, MEMBER, "teams/reviewer")
    assert daemon.plays.membership(member["id"]).position == "team-member"
    with pytest.raises(RemoteError, match="team-member cannot spawn"):
        await _spawn(client, member, MEMBER)

    resumable = daemon.registry.get(lead["id"])
    resumable.session_id, resumable.session_correlation = "sess-lead", "operator"
    daemon.store.upsert_participant(resumable)
    daemon.registry.mark_dead(lead["id"])
    with pytest.raises(RemoteError, match=f"only an agent holding {MASTER} in that play"):
        await _spawn(client, None, resume=lead["id"])
    resumed = await _spawn(client, master, resume=lead["id"])
    assert daemon.plays.membership(resumed["id"]).position == "team-lead"

    daemon.registry.mark_dead(master["id"])
    assert daemon.plays.membership(member["id"]) is None, "the master's death ends the play"
    await _spawn(client, member)
    assert (await _load(client, member["id"]))["state"] == "started", "a former member may start"


async def test_a_deferred_spawn_is_checked_again_when_it_launches(
    client, daemon, terminal_provider
):
    root = daemon.registry.create_spawned(harness="vibe", cwd="/tmp")
    _, source = _source(daemon)
    waiting = await _spawn(client, {"id": root.id}, after="Writer")

    assert (await _load(client, root.id))["state"] == "started"
    with pytest.raises(RemoteError, match="exactly one position"):
        await _spawn(client, {"id": root.id}, after="Writer")
    daemon.jobs.finish(source.handle, state=JobState.DONE, result="ready")
    await daemon.handoff.settle()

    gate = daemon.store.get_handoff_gate(waiting["handle"])
    assert gate.state == "failed" and "teams/team-lead" in (gate.error or "")
    assert daemon.store.get_job(waiting["handle"]).state == JobState.CRASHED


def _sender(client, master):
    async def send(target, *skills, caller=master, **extra):
        return await client.call(
            "send", target=target.id, caller_id=caller.id, prompt="lead", skills=[*skills], **extra
        )

    return send


async def test_a_parents_send_assigns_a_position_only_once_delivered(
    client, daemon, absent, monkeypatch
):
    master, child, state = await _native_pair(daemon)
    _, source = _source(daemon)
    send = _sender(client, master)

    with pytest.raises(RemoteError, match=f"is in no live play, so no play can seat {LEAD}"):
        await send(child, LEAD)
    assert (await _load(client, master.id))["state"] == "started"
    for skills, needle in [
        (("teams/reviewer",), "a task role has nothing to fit"),
        ((MASTER,), "a send never starts a play"),
    ]:
        with pytest.raises(RemoteError, match=needle):
            await send(child, *skills)
    assert state.sent == [], "a refused send queues nothing"

    reply = await send(child, LEAD, after="Writer")
    assert reply["delivery"] == "queued" and daemon.plays.membership(child.id) is None
    with pytest.raises(PlayRefused, match="holds no position"):
        daemon.plays.check_spawn(child.id, PlayPlan(names=(MEMBER,)))
    daemon.jobs.finish(source.handle, state=JobState.DONE, result="ready")
    await _pump(daemon, child, state)
    assert len(state.sent) == 1 and daemon.plays.membership(child.id).position == "team-lead"
    assert daemon.plays.check_spawn(child.id, PlayPlan(names=(MEMBER,))).position == "team-member"
    with pytest.raises(RemoteError, match="already holds teams/team-lead"):
        await send(child, MEMBER)

    others = [daemon.registry.create_spawned(harness="vibe", cwd="/tmp", parent_id=master.id)]
    others.append(daemon.registry.create_spawned(harness="vibe", cwd="/tmp", parent_id=master.id))
    for other in others:
        await _install_runtime(daemon, other.id)
    with pytest.raises(RemoteError, match="can give its children only: teams/team-lead"):
        await send(others[0], MEMBER)
    with pytest.raises(RemoteError, match="direct parent"):
        await send(others[0], LEAD, caller=child)
    _source(daemon, name="Writer2")
    cancelled = await send(others[0], LEAD, after="Writer2")
    assert daemon.store.plays.assignment(cancelled["handle"]) is not None
    await _cancel(daemon, {"handle": cancelled["handle"], "caller_id": master.id})
    assert daemon.store.plays.assignment(cancelled["handle"]) is None
    assert daemon.plays.membership(others[0].id) is None, "a cancelled send assigns nothing"
    seated: list[bool] = []
    track = daemon.plays.track_assignment

    def spy(assignment, handle):
        seated.append(daemon.plays.membership(assignment.participant_id) is not None)
        track(assignment, handle)

    monkeypatch.setattr(daemon.plays, "track_assignment", spy)
    await send(others[1], LEAD)
    assert seated == [False, True], "held at reservation, applied by the accepting settlement"
    assert daemon.plays.membership(others[1].id).position == "team-lead"


async def test_queued_play_messages_fail_unsent_once_their_play_ends(client, daemon, absent):
    master, child, state = await _native_pair(daemon)
    send = _sender(client, master)
    assert (await _load(client, master.id))["state"] == "started"
    await send(child, LEAD)
    late = daemon.registry.create_spawned(harness="vibe", cwd="/tmp", parent_id=master.id)
    late_state = await _install_runtime(daemon, late.id)
    _, source = _source(daemon)
    queued = [await send(late, LEAD, after="Writer")]
    queued.append(await send(child, "teams/reviewer", after="Writer"))

    daemon.registry.mark_dead(master.id)
    daemon.jobs.finish(source.handle, state=JobState.DONE, result="ready")
    await _pump(daemon, late, late_state)
    await _pump(daemon, child, state)

    for message in queued:
        job = daemon.store.get_job(message["handle"])
        assert (job.state, job.error_code) == (JobState.CRASHED, "play_refused"), job.result
        assert "has ended; nothing was delivered" in job.result
    assert late_state.sent == [] and len(state.sent) == 1, "an ended play's messages stay unsent"
    assert daemon.plays.membership(late.id) is None


async def test_adoption_keeps_one_live_play_and_its_position_hierarchy(client, daemon):
    master = daemon.registry.create_spawned(harness="vibe", cwd="/tmp")
    await _load(client, master.id)
    lead_a, lead_b, loose = (
        daemon.registry.create_spawned(harness="vibe", cwd="/tmp", parent_id=master.id)
        for _ in range(3)
    )
    for lead in (lead_a, lead_b):
        _seat(daemon, lead.id, master.id, "team-lead")
    outsider = daemon.registry.create_spawned(harness="vibe", cwd="/tmp")
    inner = daemon.registry.create_spawned(harness="vibe", cwd="/tmp", parent_id=outsider.id)
    await _load(client, inner.id)
    for target in (lead_b, loose, outsider):
        await _install_runtime(daemon, target.id)

    async def adopt(target, caller):
        return await client.call("participant.adopt", id=target.id, caller_id=caller.id)

    with pytest.raises(RemoteError, match="may parent only teams/team-member"):
        await adopt(lead_b, lead_a)
    with pytest.raises(RemoteError, match="you hold no position there"):
        await adopt(lead_b, loose)
    with pytest.raises(RemoteError, match="one play cannot sit inside another"):
        await adopt(outsider, master)
    assert daemon.registry.get(lead_b.id).parent_id == master.id
    await adopt(loose, lead_a)
    assert daemon.registry.get(loose.id).parent_id == lead_a.id, "its new parent assigns it"


async def test_gc_drops_a_deleted_masters_play_rows(client, daemon):
    master = daemon.registry.create_spawned(harness="vibe", cwd="/tmp")
    await _load(client, master.id)

    with daemon.store.write_unit() as unit:
        _delete_participant_dependents(unit.connection, master.id)

    for table, column in ((play_instances, "master_id"), (play_members, "master_id")):
        rows = daemon.store.conn.execute(select(table).where(table.c[column] == master.id))
        assert rows.first() is None
