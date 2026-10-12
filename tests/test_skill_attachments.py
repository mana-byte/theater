"""``skills=[...]`` on spawn and send: resolved at admission, carded after the task, bounded."""

from __future__ import annotations

import dataclasses

import pytest

from tests._presence_doubles import AbsentPresence
from tests.test_control_rpc import _GUIDANCE, _active_send, _native_pair, _until
from theater import paths
from theater.config.models import SkillsSection
from theater.constants.skills import SKILL_ATTACH_MAX_BYTES
from theater.daemon.skill_cards import SKILL_CLOSE, SKILL_OPEN
from theater.protocol import RemoteError


@pytest.fixture(autouse=True)
async def _absent_presence(daemon):
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(daemon, "presence", AbsentPresence(), raising=False)
        yield


async def test_send_attaches_skill_cards_between_the_task_and_response_guidance(client, daemon):
    parent, child, state = await _native_pair(daemon)

    await client.call(
        "send",
        target=child.id,
        prompt="Audit module A",
        caller_id=parent.id,
        skills=["theater-debate", "theater-debate"],
        response_format={"$theater": "delegate"},
    )

    assert await _until(lambda: state.sent)
    (delivered,) = state.sent
    sender = daemon.store.get_participant(parent.id)
    card = delivered.index(f"{SKILL_OPEN} theater-debate (builtin, sha256:")
    assert delivered.startswith("Audit module A\n\n"), "the task leads, so turn matching holds"
    assert f"attached by {sender.name or sender.id}." in delivered
    assert delivered.count(SKILL_OPEN) == 1, "a repeated name attaches once"
    assert "\nname: theater-debate\n" not in delivered, "the frontmatter is not attached"
    assert card < delivered.index(SKILL_CLOSE) < delivered.index(_GUIDANCE)

    plain = await client.call(
        "send", target=child.id, prompt="plain words", caller_id=parent.id, skills=[]
    )
    assert daemon.store.get_job(plain["handle"]).prompt == "plain words", "empty means none"


async def test_queued_sends_keep_their_skills_and_amend_refuses_them(client, daemon):
    parent, child, state = await _native_pair(daemon)
    active = await _active_send(client, parent, child)

    queued = await client.call(
        "send",
        target=child.id,
        prompt="next step",
        caller_id=parent.id,
        if_busy="queue",
        skills=["theater-debate"],
    )
    assert queued["route"] == "queued"
    prompt = daemon.store.get_job(queued["handle"]).prompt
    assert prompt.startswith("next step\n\n") and f"{SKILL_OPEN} theater-debate" in prompt

    with pytest.raises(RemoteError, match="cannot attach skills to an active turn"):
        await client.call(
            "send",
            target=child.id,
            prompt="steer now",
            caller_id=parent.id,
            if_busy="amend",
            job_handle=active["handle"],
            skills=["theater-debate"],
        )
    assert state.steered == []


async def test_skill_attachments_are_refused_before_anything_is_created(
    client, daemon, monkeypatch
):
    parent, child, _state = await _native_pair(daemon)
    package = paths.skills_dir() / "huge-guide"
    package.mkdir(parents=True)
    (package / "SKILL.md").write_text(
        "---\nname: huge-guide\ndescription: Too long to attach.\n---\n\n"
        + "x" * SKILL_ATTACH_MAX_BYTES
        + "\n",
        encoding="utf-8",
    )

    async def send(**extra):
        return await client.call(
            "send", target=child.id, caller_id=parent.id, **{"prompt": "task", **extra}
        )

    cases = [
        ({"skills": "theater-debate"}, "bad_request", "list of skill names"),
        ({"skills": ["Not A Name"]}, "bad_request", "canonical skill name"),
        ({"skills": ["missing-skill"]}, "not_found", "call list_skills"),
        ({"skills": ["theater-debate"], "prompt": "   "}, "bad_request", "needs a prompt"),
        ({"skills": ["huge-guide"]}, "bad_request", f"{SKILL_ATTACH_MAX_BYTES}-byte bound"),
    ]
    for extra, code, message in cases:
        with pytest.raises(RemoteError) as refused:
            await send(**extra)
        assert (refused.value.code, message in refused.value.message) == (code, True), extra

    with pytest.raises(RemoteError, match="cannot resume a session with skills"):
        await client.call(
            "spawn",
            harness="opencode",
            approval="yolo",
            cwd="/tmp",
            prompt="carry on",
            resume="ses_1",
            skills=["theater-debate"],
        )

    disabled = SkillsSection(disabled=("theater-debate",))
    monkeypatch.setattr(daemon, "config", dataclasses.replace(daemon.config, skills=disabled))
    with pytest.raises(RemoteError) as refused:
        await send(skills=["theater-debate"])
    assert refused.value.code == "not_found"
    assert daemon.store.running_jobs_for_target(child.id) == []
