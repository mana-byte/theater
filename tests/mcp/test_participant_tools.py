"""Participant tool bodies: identify, whoami, list, update, terminate and adopt."""

from __future__ import annotations

import asyncio

import pytest

from tests.support.mcp_tools import RECORD, FakeClient, resolved, session
from theater.constants.daemon import PARTICIPANTS_LIST_DEFAULT_DEAD_LIMIT
from theater.mcp import tools


async def test_parallel_initial_calls_register_one_participant():
    class SlowHello(FakeClient):
        async def call(self, method, **params):
            await asyncio.sleep(0)
            return await super().call(method, **params)

    client = SlowHello()
    s = tools.Session(participant_id=None, harness="vibe", client=client)
    records = await asyncio.gather(s.identify(), s.me(), s.identify())
    assert client.methods.count("hello") == 1
    assert client.methods.count("participants.get") == 2
    assert all(record["id"] == RECORD["id"] for record in records)


async def test_identify_does_not_report_legacy_pane_identity(monkeypatch):
    monkeypatch.setenv("TMUX_PANE", "%9")
    s = session()
    await s.identify()
    assert "pane" not in s.client.params("hello")
    assert s._resolved


async def test_identify_accepts_the_id_the_daemon_hands_back():
    """The daemon may file us under a different id than the one on argv."""
    s = session(hello={**RECORD, "id": "p-other"})
    assert await s.caller_id() == "p-other"
    assert s.participant_id == "p-other"
    assert s.client.methods == ["hello"]


async def test_me_re_reads_the_record_once_identity_is_settled():
    """Returning the cached record would report a status frozen at start-up."""
    s = resolved()
    await s.me()
    assert s.client.methods == ["participants.get"]


async def test_me_identifies_first_when_it_has_not_yet():
    s = session()
    await s.me()
    assert s.client.methods == ["hello"]


async def test_whoami_answers_who_where_and_reachable():
    s = resolved(**{"participants.list": []})
    got = await tools.whoami(s)
    assert s.client.params("participants.get") == {"id": "p-me", "self_view": True}
    assert got == {
        "id": "p-me",
        "name": "Arlequin",
        "description": "Coordinate the implementation",
        "harness": "vibe",
        "tier": "spawned",
        "status": "idle",
        "cwd": "/tmp/project",
        "session_id": "ses-me",
        "addressable": True,
    }
    assert "pid" not in got, "process detail is noise to another agent"
    assert got["name"] == "Arlequin"


async def test_whoami_include_git_adds_the_live_observation_with_its_nulls():
    git = {"pin_state": "pinned", "commit_sha": "abc", "branch": None, "dirty_entries": 0}
    record = {**RECORD, "branch": "rc10/integration", "git": {**git, "reason": None}}
    s = resolved(**{"participants.get": record, "participants.list": []})
    got = await tools.whoami(s, include_git=True)
    assert s.client.params("participants.get") == {
        "id": "p-me",
        "self_view": True,
        "include_git": True,
    }
    assert got["branch"] == "rc10/integration"
    assert got["git"] == {**git, "reason": None}, "detached HEAD's null branch is the answer"


async def test_whoami_omits_a_lazy_session_id_until_discovered():
    got = await tools.whoami(
        resolved(**{"participants.get": {**RECORD, "session_id": None}, "participants.list": []})
    )
    assert "session_id" not in got


async def test_whoami_lists_live_direct_children_capped():
    rows = [{**RECORD, "id": f"c{i}", "name": f"Kid-{i}", "parent_id": "p-me"} for i in range(21)]
    s = resolved(**{"participants.list": rows})
    got = await tools.whoami(s)
    assert s.client.params("participants.list") == {"parent_id": "p-me"}
    assert got["children"][0] == {"name": "Kid-0", "status": "idle", "harness": "vibe"}
    assert len(got["children"]) == 20
    assert got["children_total"] == 21


async def test_list_participants_marks_which_row_is_the_caller():
    """Without this an agent can send a prompt to itself and wait on its own turn."""
    rows = [
        {**RECORD, "id": "p-me"},
        {**RECORD, "id": "p-other", "session_id": None},
    ]
    got = await tools.list_participants(resolved(**{"participants.list": rows}))
    assert got[0]["is_self"] is True
    assert "is_self" not in got[1]
    assert [r.get("session_id") for r in got] == ["ses-me", None]


async def test_list_participants_identifies_before_it_can_say_who_is_self():
    s = session(**{"participants.list": []})
    await tools.list_participants(s)
    assert s.client.methods == ["hello", "participants.list"]


async def test_skill_tools_group_listing_and_identify_the_loader():
    listed = {"skills": [{"name": "alpha"}], "plays": [], "rejections": []}
    loaded = {"name": "alpha", "content": "# Alpha\n"}
    s = session(**{"skills.list": listed, "skills.load": loaded})

    assert await tools.list_skills(s, play="hierarchy") == listed
    assert await tools.load_skill(s, name="alpha") == loaded
    assert s.client.methods == ["skills.list", "hello", "skills.load"]
    assert s.client.params("skills.list") == {"grouped": True, "play": "hierarchy"}
    assert s.client.params("skills.load") == {"name": "alpha", "caller_id": "p-me"}


@pytest.mark.parametrize("make_session", [session, resolved])
async def test_skill_preview_omits_identity_even_for_an_identified_session(make_session):
    loaded = {"name": "hierarchy/master", "content": "# Master\n"}
    s = make_session(**{"skills.load": loaded})

    assert await tools.load_skill(s, name="hierarchy/master", preview=True) == loaded
    assert s.client.methods == ["skills.load"]
    assert s.client.params("skills.load") == {"name": "hierarchy/master", "preview": True}


async def test_update_participant_identifies_and_forwards_metadata():
    s = session(**{"participant.update": {**RECORD, "description": "Updated"}})
    updated = await tools.update_participant(
        s,
        target="p-child",
        name="Metadata-Child",
        description="Updated",
    )
    assert updated["description"] == "Updated"
    assert s.client.params("participant.update") == {
        "caller_id": "p-me",
        "target": "p-child",
        "name": "Metadata-Child",
        "description": "Updated",
    }


async def test_terminate_session_names_the_caller_so_the_daemon_can_authorize():
    """The daemon checks parentage against caller_id, so it must reach the daemon."""
    s = resolved(**{"participant.kill": {"id": "p-child", "killed": True}})
    await tools.terminate_session(s, target="p-child")
    p = s.client.params("participant.kill")
    assert p["id"] == "p-child"
    assert p["caller_id"] == "p-me"
    assert "forget" not in p
    await tools.terminate_session(s, target="p-child", forget=True)
    assert s.client.calls[-1] == (
        "participant.kill",
        {"id": "p-child", "caller_id": "p-me", "forget": True},
    )


async def test_adopt_session_names_the_caller_so_the_daemon_can_authorize():
    """The daemon checks sibling-hood against caller_id, so it must reach the daemon."""
    s = resolved(**{"participant.adopt": {"id": "p-sib", "parent_id": "p-me"}})
    await tools.adopt_session(s, target="p-sib")
    p = s.client.params("participant.adopt")
    assert p["id"] == "p-sib"
    assert p["caller_id"] == "p-me"


_RECORD_WITH_RESUME = {
    **RECORD,
    "resume_state": "live",
}


async def test_list_participants_forwards_ids_to_daemon():
    """ids parameter is forwarded to the RPC unchanged."""
    rows = [{**_RECORD_WITH_RESUME, "id": "p-me"}]
    s = resolved(**{"participants.list": rows})
    await tools.list_participants(s, ids=["p-me", "p-other"])
    params = s.client.params("participants.list")
    assert params["ids"] == ["p-me", "p-other"]


async def test_list_participants_forwards_ids_none():
    """ids=None is forwarded (daemon treats it as absent)."""
    rows = [{**_RECORD_WITH_RESUME, "id": "p-me"}]
    s = resolved(**{"participants.list": rows})
    await tools.list_participants(s, ids=None)
    params = s.client.params("participants.list")
    assert params["ids"] is None


async def test_list_participants_forwards_keyset_pagination_to_daemon():
    rows = [{**_RECORD_WITH_RESUME, "id": "p-other"}]
    s = resolved(**{"participants.list": rows})
    await tools.list_participants(s, include_dead=True, limit=25, after_id="p-before")
    assert s.client.params("participants.list") == {
        "include_dead": True,
        "ids": None,
        "parent_id": None,
        "order": "newest",
        "limit": 25,
        "after_id": "p-before",
    }


async def test_list_participants_marks_a_full_page_with_its_cursor():
    rows = [{**_RECORD_WITH_RESUME, "id": "p-a"}, {**_RECORD_WITH_RESUME, "id": "p-b"}]
    full = await tools.list_participants(resolved(**{"participants.list": rows}), limit=2)
    short = await tools.list_participants(resolved(**{"participants.list": rows}), limit=3)
    assert full[-1]["next_after_id"] == "p-b" and "next_after_id" not in full[0]
    assert all("next_after_id" not in row for row in short)


async def test_list_participants_caps_unfiltered_dead_history_for_mcp():
    s = resolved(**{"participants.list": []})
    await tools.list_participants(s, include_dead=True)
    assert (
        s.client.params("participants.list")["limit"] == PARTICIPANTS_LIST_DEFAULT_DEAD_LIMIT == 20
    )


async def test_list_participants_keeps_mcp_dead_cap_on_cursor_pages():
    s = resolved(**{"participants.list": []})
    await tools.list_participants(s, include_dead=True, after_id="p-before")
    assert s.client.params("participants.list")["limit"] == PARTICIPANTS_LIST_DEFAULT_DEAD_LIMIT


async def test_list_participants_does_not_cap_id_or_live_queries():
    with_ids = resolved(**{"participants.list": []})
    await tools.list_participants(with_ids, include_dead=True, ids=["p-dead"])
    assert "limit" not in with_ids.client.params("participants.list")

    live = resolved(**{"participants.list": []})
    await tools.list_participants(live)
    assert "limit" not in live.client.params("participants.list")


async def test_list_participants_scopes_children_to_caller():
    rows = [{**_RECORD_WITH_RESUME, "id": "p-child", "parent_id": "p-me"}]
    s = resolved(**{"participants.list": rows})
    got = await tools.list_participants(s, children_only=True)
    params = s.client.params("participants.list")
    assert params["parent_id"] == "p-me"
    assert [row["id"] for row in got] == ["p-child"]


async def test_list_participants_default_has_no_parent_scope():
    s = resolved(**{"participants.list": []})
    await tools.list_participants(s)
    assert s.client.params("participants.list")["parent_id"] is None


async def test_list_participants_resume_state_in_projection():
    """Live rows need no resume verdict; dead rows need no false route flag."""
    rows = [
        {**_RECORD_WITH_RESUME, "id": "p-me", "addressable": False},
        {
            **_RECORD_WITH_RESUME,
            "id": "p-other",
            "status": "dead",
            "addressable": False,
            "resume_state": "resumable",
        },
    ]
    s = resolved(**{"participants.list": rows})
    got = await tools.list_participants(s)
    assert "resume_state" not in got[0]
    assert got[0]["addressable"] is False
    assert "addressable" not in got[1]
    assert got[1]["resume_state"] == "resumable"


async def test_list_participants_projects_tmux_restart_diagnosis():
    row = {
        **_RECORD_WITH_RESUME,
        "tmux_server_identity": "server-before",
        "termination_reason": "tmux_restart",
        "termination_incident": "incident-123",
        "terminated_at": 123.0,
    }
    got = await tools.list_participants(resolved(**{"participants.list": [row]}), include_dead=True)
    assert got[0]["tmux_server_identity"] == "server-before"
    assert got[0]["termination_reason"] == "tmux_restart"
    assert got[0]["termination_incident"] == "incident-123"
    assert got[0]["terminated_at"] == 123.0


async def test_list_participants_no_internal_fields_in_projection():
    """Internal fields must not bleed through _summarise even if the daemon returned them."""
    row = {
        **_RECORD_WITH_RESUME,
        "session_correlation": "operator",
        "transcript_domain": "/tmp/d",
        "transcript_location": "/tmp/d/m.jsonl",
        "resume_floor": "abc",
    }
    s = resolved(**{"participants.list": [row]})
    got = await tools.list_participants(s)
    assert len(got) == 1
    for field in (
        "session_correlation",
        "transcript_domain",
        "transcript_location",
        "resume_floor",
    ):
        assert field not in got[0], f"internal field {field!r} leaked through"


# Each tool body says hello first, or the daemon cannot name the caller.
CASES = {
    # the reply has nowhere to go without an identity
    "send": (
        {"send": {"handle": "p-you#1"}},
        lambda s: tools.send_prompt(s, target_id="p-you", prompt="hello"),
        ["hello", "send"],
    ),
    "update_session_settings": (
        {"participant.settings.update": {"applied": True}},
        lambda s: tools.update_session_settings(s, target="p-child", model="opus-5"),
        ["hello", "participant.settings.update"],
    ),
    "get_session_controls": (
        {"participant.controls": {}},
        lambda s: tools.get_session_controls(s, target="p-child"),
        ["hello", "participant.controls"],
    ),
    # the daemon authorizes by the named caller
    "terminate_session": (
        {"participant.kill": {"id": "p-child", "killed": True}},
        lambda s: tools.terminate_session(s, target="p-child"),
        ["hello", "participant.kill"],
    ),
    "adopt_session": (
        {"participant.adopt": {"id": "p-sib", "parent_id": "p-me"}},
        lambda s: tools.adopt_session(s, target="p-sib"),
        ["hello", "participant.adopt"],
    ),
    # caller_cwd is what the daemon uses for the privacy wall
    "recall": ({"recall": {}}, lambda s: tools.recall(s, paths=["x.py"]), ["hello", "recall"]),
    "recall_read": (
        {"recall_read": {}},
        lambda s: tools.recall_read(s, segment_id="codex-a41f"),
        ["hello", "recall_read"],
    ),
}


@pytest.mark.parametrize("tool", CASES)
async def test_a_tool_identifies_before_its_first_daemon_call(tool):
    replies, call, methods = CASES[tool]
    s = session(**replies)
    await call(s)
    assert s.client.methods == methods
