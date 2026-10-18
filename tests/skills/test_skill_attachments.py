"""``skills=[...]`` on spawn and send: resolved at admission, carded after the task, bounded."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from tests.support.native_rpc import GUIDANCE, active_send, native_pair
from tests.support.waiting import until
from theater import paths
from theater.config.models import SkillsSection
from theater.constants.skills import SKILL_ATTACH_MAX_BYTES
from theater.daemon.observation.turns import answers_prompt
from theater.daemon.skill_cards import SKILL_CLOSE, SKILL_OPEN, compose_prompt, render_skills
from theater.protocol import RemoteError
from theater.skills import Skill, SkillSource

pytestmark = pytest.mark.usefixtures("absent_presence")


def _user_skill(name: str, body: str) -> None:
    package = paths.skills_dir() / name
    package.mkdir(parents=True)
    content = f"---\nname: {name}\ndescription: Attachable guide.\n---\n\n{body}\n"
    (package / "SKILL.md").write_text(content, encoding="utf-8")


def test_skill_cards_follow_the_task_so_turn_matching_still_tells_tasks_apart():
    guide = Skill(
        "guide",
        "Guide.",
        "---\nname: guide\ndescription: Guide.\n---\n\nFollow the guide.\n",
        SkillSource.USER,
        Path("/tmp/skills/guide/SKILL.md"),
    )
    cards = render_skills([guide], attached_by="the operator")
    first = compose_prompt("Audit module A", skills=cards)
    second = compose_prompt("Audit module B", skills=cards)

    assert answers_prompt([first], first) and not answers_prompt([second], first)
    hinted = compose_prompt("Audit module A", skills=cards, response_format='{"type":"object"}')
    assert hinted.startswith(GUIDANCE), "a bare hint keeps its prepend"
    assert hinted.index("Audit module A") < hinted.index(SKILL_OPEN)


async def test_spawn_carries_skill_cards_on_the_childs_command_line(
    client, daemon, terminal_provider
):
    record = await client.call(
        "spawn",
        harness="vibe",
        approval="manual",
        cwd="/tmp",
        prompt="Build the parser",
        skills=["theater-debate"],
    )

    prompt = daemon.store.get_job(record["handle"]).prompt
    assert prompt.startswith("Build the parser\n\n")
    assert f"{SKILL_OPEN} theater-debate (builtin, sha256:" in prompt
    assert "attached by the operator." in prompt
    assert prompt in terminal_provider.creations[-1]["command"]


async def test_send_attaches_skill_cards_between_the_task_and_response_guidance(client, daemon):
    parent, child, state = await native_pair(daemon)

    await client.call(
        "send",
        target=child.id,
        prompt="Audit module A",
        caller_id=parent.id,
        skills=["theater-debate", "theater-debate"],
        response_format={"$theater": "delegate"},
    )

    assert await until(lambda: state.sent)
    (delivered,) = state.sent
    sender = daemon.store.get_participant(parent.id)
    card = delivered.index(f"{SKILL_OPEN} theater-debate (builtin, sha256:")
    assert delivered.startswith("Audit module A\n\n"), "the task leads, so turn matching holds"
    assert f"attached by {sender.name or sender.id}." in delivered
    assert delivered.count(SKILL_OPEN) == 1, "a repeated name attaches once"
    assert "\nname: theater-debate\n" not in delivered, "the frontmatter is not attached"
    assert card < delivered.index(SKILL_CLOSE) < delivered.index(GUIDANCE)

    plain = await client.call(
        "send", target=child.id, prompt="plain words", caller_id=parent.id, skills=[]
    )
    assert daemon.store.get_job(plain["handle"]).prompt == "plain words", "empty means none"


async def test_queued_sends_keep_their_skills_and_amend_refuses_them(client, daemon):
    parent, child, state = await native_pair(daemon)
    active = await active_send(client, parent, child)

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
    parent, child, _state = await native_pair(daemon)
    for name in ("half-a", "half-b"):
        _user_skill(name, "x" * (SKILL_ATTACH_MAX_BYTES // 2))

    async def send(**extra):
        return await client.call(
            "send", target=child.id, caller_id=parent.id, **{"prompt": "task", **extra}
        )

    cases = [
        ({"skills": "theater-debate"}, "bad_request", "list of skill names"),
        ({"skills": ["Not A Name"]}, "bad_request", "canonical skill name"),
        ({"skills": ["missing-skill"]}, "not_found", "call list_skills"),
        ({"skills": ["theater-debate"], "prompt": "   "}, "bad_request", "needs a prompt"),
        ({"skills": ["half-a", "half-b"]}, "bad_request", f"{SKILL_ATTACH_MAX_BYTES}-byte bound"),
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

    accepted = await send(skills=["half-a"])
    assert "x" * 100 in daemon.store.get_job(accepted["handle"]).prompt, "the bound is combined"
