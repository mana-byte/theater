"""The tool bodies an agent reaches through MCP.

The framework wrapper is covered in test_mcp_server.py. What can be wrong here
is quieter: a tool that forgets to identify itself first is filed as External
and becomes unaddressable, and one that forgets to name its caller defeats the
deadlock rail — neither shows up as an error, only as work that never lands.
"""

from __future__ import annotations

from theater.constants.daemon import PARTICIPANTS_LIST_DEFAULT_DEAD_LIMIT
from theater.mcp import tools

RECORD = {
    "id": "p-me",
    "name": "Arlequin",
    "harness": "vibe",
    "tier": "spawned",
    "status": "idle",
    "cwd": "/tmp/project",
    "branch": None,
    "session_id": "ses-me",
    "parent_id": None,
    "addressable": True,
    "tmux_pane": "%3",
    "pid": 4321,
    "resume_state": "live",
}


class FakeClient:
    """A daemon that answers from a dict and remembers what it was asked."""

    def __init__(self, **replies):
        self.replies = replies
        self.calls: list[tuple[str, dict]] = []

    async def call(self, method, **params):
        self.calls.append((method, params))
        return self.replies.get(method, RECORD)

    def params(self, method: str) -> dict:
        return next(p for m, p in self.calls if m == method)

    @property
    def methods(self) -> list[str]:
        return [m for m, _ in self.calls]


def session(**replies) -> tools.Session:
    return tools.Session(participant_id="p-me", harness="vibe", client=FakeClient(**replies))


def resolved(**replies) -> tools.Session:
    s = session(**replies)
    s._resolved = True
    return s


async def test_identify_reports_the_pane_from_the_environment(monkeypatch):
    monkeypatch.setenv("TMUX_PANE", "%9")
    s = session()
    await s.identify()
    assert s.client.params("hello")["pane"] == "%9"
    assert s._resolved


async def test_identify_accepts_the_id_the_daemon_hands_back():
    """The daemon may file us under a different id than the one on argv."""
    s = session(hello={**RECORD, "id": "p-other"})
    await s.identify()
    assert s.participant_id == "p-other"


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
    got = await tools.whoami(resolved())
    assert got == {
        "id": "p-me",
        "name": "Arlequin",
        "harness": "vibe",
        "tier": "spawned",
        "status": "idle",
        "cwd": "/tmp/project",
        "branch": None,
        "session_id": "ses-me",
        "parent_id": None,
        "addressable": True,
    }
    assert "pid" not in got, "process detail is noise to another agent"
    assert got["name"] == "Arlequin"


async def test_whoami_keeps_a_lazy_session_id_as_null():
    got = await tools.whoami(resolved(**{"participants.get": {**RECORD, "session_id": None}}))
    assert "session_id" in got
    assert got["session_id"] is None


async def test_list_participants_marks_which_row_is_the_caller():
    """Without this an agent can send a prompt to itself and wait on its own turn."""
    rows = [
        {**RECORD, "id": "p-me"},
        {**RECORD, "id": "p-other", "session_id": None},
    ]
    got = await tools.list_participants(resolved(**{"participants.list": rows}))
    assert [r["is_self"] for r in got] == [True, False]
    assert [r["session_id"] for r in got] == ["ses-me", None]


async def test_list_participants_identifies_before_it_can_say_who_is_self():
    s = session(**{"participants.list": []})
    await tools.list_participants(s)
    assert s.client.methods == ["hello", "participants.list"]


async def test_harnesses_hides_the_ones_that_cannot_be_spawned():
    """Offering a harness the daemon would refuse only invites a failed call."""
    rows = [
        {"name": "vibe", "icon": "V", "binary": "vibe", "installed": True, "error": None},
        {"name": "gone", "icon": "G", "binary": "gone", "installed": False, "error": None},
        {"name": "broken", "icon": "B", "binary": "b", "installed": True, "error": "boom"},
    ]
    got = await tools.harnesses(resolved(harnesses=rows))
    assert [r["name"] for r in got] == ["vibe"]
    assert "installed" not in got[0]


async def test_skill_tools_forward_only_the_skills_rpc_methods():
    listed = {"skills": [{"name": "alpha"}], "rejections": []}
    loaded = {"name": "alpha", "content": "# Alpha\n"}
    s = session(**{"skills.list": listed, "skills.load": loaded})

    assert await tools.list_skills(s) == listed
    assert await tools.load_skill(s, name="alpha") == loaded
    assert s.client.calls == [
        ("skills.list", {}),
        ("skills.load", {"name": "alpha"}),
    ]


async def test_spawn_names_the_caller_as_the_parent():
    """Lineage is set here or never — the child cannot know who asked for it."""
    s = resolved()
    child = await tools.spawn_session(s, harness="vibe", prompt="hi", approval="manual")
    assert s.client.params("spawn")["parent_id"] == "p-me"
    assert child["session_id"] == "ses-me"


async def test_spawn_accepts_no_prompt():
    """No prompt means the daemon starts a plain CLI and resolves the job."""
    s = resolved()
    await tools.spawn_session(s, harness="vibe", approval="manual")
    assert s.client.params("spawn")["prompt"] is None


async def test_spawn_forwards_response_format_unchanged():
    """MCP forwards the schema hint to the daemon; it does not serialize it."""
    response_format = {
        "type": "object",
        "properties": {"answer": {"type": "string"}},
        "required": ["answer"],
    }
    s = resolved()
    await tools.spawn_session(
        s,
        harness="vibe",
        prompt="answer in JSON",
        response_format=response_format,
        approval="manual",
    )
    assert s.client.params("spawn")["response_format"] is response_format


async def test_spawn_forwards_empty_response_format():
    """An empty schema object is still an explicit response_format."""
    response_format = {}
    s = resolved()
    await tools.spawn_session(
        s,
        harness="vibe",
        prompt="answer in JSON",
        response_format=response_format,
        approval="manual",
    )
    assert s.client.params("spawn")["response_format"] is response_format


async def test_spawn_defaults_the_cwd_to_this_process():
    s = resolved()
    await tools.spawn_session(s, harness="vibe", prompt="hi", approval="manual")
    assert s.client.params("spawn")["cwd"]


async def test_register_pane_settles_identity_on_the_pane_it_was_told():
    """The MCP env allowlist hides TMUX_PANE, so the agent reads it and tells us."""
    s = session(hello={**RECORD, "id": "p-adopted"})
    adopted = await tools.register_pane(s, pane="%12")
    assert s.client.params("hello")["pane"] == "%12"
    assert (s.participant_id, s._resolved) == ("p-adopted", True)
    assert adopted["session_id"] == "ses-me"


async def test_await_names_the_caller_so_a_deadlock_can_be_refused():
    """Waiting on someone who is waiting on you has to be visible to the daemon."""
    s = resolved(**{"jobs.await": []})
    await tools.await_sessions(s, handles=["h#1"], max_wait=5.0)
    p = s.client.params("jobs.await")
    assert (p["caller_id"], p["handles"], p["max_wait"]) == ("p-me", ["h#1"], 5.0)


async def test_await_drops_prompt_and_result_from_the_agent_facing_shape():
    """The prompt is what the caller already sent; result is a clip. An agent

    that wants either reads the transcript, so neither belongs in the await
    reply — but the routing fields do.
    """
    job = {
        "handle": "h#1",
        "caller_id": "p-me",
        "target_id": "p-you",
        "kind": "send",
        "prompt": "do the thing",
        "response_format": {"type": "object"},
        "state": "done",
        "result": "a 2000-char clip nobody asked for",
        "structured_result": '{"answer": 42}',
        "structured_status": "parsed",
        "error_code": None,
        "created_at": 1.0,
        "finished_at": 2.0,
    }
    s = resolved(**{"jobs.await": [job]})
    jobs = await tools.await_sessions(s, handles=["h#1"], max_wait=5.0)
    assert "prompt" not in jobs[0]
    assert "result" not in jobs[0]
    assert (jobs[0]["handle"], jobs[0]["state"], jobs[0]["error_code"]) == ("h#1", "done", None)
    assert jobs[0]["response_format"] == {"type": "object"}
    assert jobs[0]["structured_result"] == '{"answer": 42}'
    assert jobs[0]["structured_status"] == "parsed"


async def test_send_names_the_caller_so_the_reply_comes_back():
    s = resolved(send={"handle": "p-you#1"})
    await tools.send_prompt(s, target_id="p-you", prompt="hello")
    p = s.client.params("send")
    assert (p["target"], p["prompt"], p["caller_id"]) == ("p-you", "hello", "p-me")


async def test_send_forwards_response_format_unchanged():
    response_format = {
        "type": "object",
        "properties": {"ok": {"type": "boolean"}},
    }
    s = resolved(send={"handle": "p-you#1"})
    await tools.send_prompt(
        s,
        target_id="p-you",
        prompt="hello",
        response_format=response_format,
    )
    assert s.client.params("send")["response_format"] is response_format


async def test_send_forwards_empty_response_format():
    response_format = {}
    s = resolved(send={"handle": "p-you#1"})
    await tools.send_prompt(
        s,
        target_id="p-you",
        prompt="hello",
        response_format=response_format,
    )
    assert s.client.params("send")["response_format"] is response_format


async def test_send_identifies_first_or_the_reply_has_nowhere_to_go():
    s = session(send={"handle": "p-you#1"})
    await tools.send_prompt(s, target_id="p-you", prompt="hello")
    assert s.client.methods == ["hello", "send"]


async def test_scratchpad_write_identifies_and_forwards_exact_rpc_arguments():
    s = session(**{"scratchpad.write": {"namespace": "plan", "key": "abc123"}})
    got = await tools.scratchpad_write(s, value="p-you", namespace="plan")
    assert got == {"namespace": "plan", "key": "abc123"}
    assert s.client.methods == ["hello", "scratchpad.write"]
    assert s.client.params("scratchpad.write") == {
        "namespace": "plan",
        "value": "p-you",
        "key": None,
        "caller_id": "p-me",
    }


async def test_scratchpad_get_identifies_and_forwards_exact_rpc_arguments():
    s = session(**{"scratchpad.get": {"namespace": "plan", "entries": {"k1": "v1"}}})
    got = await tools.scratchpad_get(s, namespace="plan")
    assert got == {"namespace": "plan", "entries": {"k1": "v1"}}
    assert s.client.methods == ["hello", "scratchpad.get"]
    assert s.client.params("scratchpad.get") == {
        "namespace": "plan",
        "keys": None,
        "caller_id": "p-me",
    }


async def test_read_transcript_forwards_a_live_name_without_listing_first():
    s = resolved(read_transcript={"id": "p-you", "events": []})
    await tools.read_transcript(s, target="Arlequin")
    assert s.client.methods == ["read_transcript"]
    assert s.client.params("read_transcript") == {"id": "Arlequin"}


async def test_read_transcript_forwards_only_the_returned_cursor():
    s = resolved(read_transcript={"id": "p-you", "events": []})
    await tools.read_transcript(s, target="Arlequin", cursor="trc1.cursor")
    assert s.client.params("read_transcript") == {
        "id": "Arlequin",
        "cursor": "trc1.cursor",
    }


async def test_put_child_back_in_the_wound_names_the_caller_so_the_daemon_can_authorize():
    """The daemon checks parentage against caller_id, so it must reach the daemon."""
    s = resolved(**{"participant.kill": {"id": "p-child", "killed": True}})
    await tools.put_child_back_in_the_wound(s, target_id="p-child")
    p = s.client.params("participant.kill")
    assert p["id"] == "p-child"
    assert p["caller_id"] == "p-me"


async def test_put_child_back_in_the_wound_identifies_first_or_the_daemon_cannot_authorize():
    s = session(**{"participant.kill": {"id": "p-child", "killed": True}})
    await tools.put_child_back_in_the_wound(s, target_id="p-child")
    assert s.client.methods == ["hello", "participant.kill"]


async def test_recall_passes_paths_and_depth_to_the_daemon():
    """The daemon does the query; the tool body just forwards the args."""
    s = resolved(recall={"src/main.py": {"timeline": []}})
    await tools.recall(s, paths=["src/main.py"], depth=3)
    p = s.client.params("recall")
    assert p["paths"] == ["src/main.py"]
    assert p["depth"] == 3
    assert p["caller_cwd"]  # the tool sends its own cwd


async def test_recall_identifies_first_so_the_daemon_knows_the_caller():
    """The caller_cwd is what the daemon uses for the privacy wall."""
    s = session(recall={})
    await tools.recall(s, paths=["x.py"])
    assert s.client.methods == ["hello", "recall"]


async def test_recall_read_forwards_the_segment_id_verbatim():
    """The segment id is a contract between recall and recall_read.

    A gap id is ``gap:<path>:<before>..<after>``, so it carries colons
    and dots that any normalising on the way through would corrupt.
    """
    seg = "gap:theater/daemon/store.py:2a0ff31..e91c4d2"
    s = resolved(recall_read={"kind": "gap"})
    await tools.recall_read(s, segment_id=seg)
    p = s.client.params("recall_read")
    assert p["segment_id"] == seg
    assert p["caller_cwd"]  # the git root for the privacy wall


async def test_recall_read_identifies_first():
    s = session(recall_read={})
    await tools.recall_read(s, segment_id="codex-a41f")
    assert s.client.methods == ["hello", "recall_read"]


# ---- list_participants: ids + resume_state projection ---------------------

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
        "limit": 25,
        "after_id": "p-before",
    }


async def test_list_participants_caps_unfiltered_dead_history_for_mcp():
    s = resolved(**{"participants.list": []})
    await tools.list_participants(s, include_dead=True)
    assert s.client.params("participants.list")["limit"] == PARTICIPANTS_LIST_DEFAULT_DEAD_LIMIT


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
    """resume_state appears in each returned row."""
    rows = [
        {**_RECORD_WITH_RESUME, "id": "p-me", "resume_state": "live"},
        {**_RECORD_WITH_RESUME, "id": "p-other", "resume_state": "resumable"},
    ]
    s = resolved(**{"participants.list": rows})
    got = await tools.list_participants(s)
    assert got[0]["resume_state"] == "live"
    assert got[1]["resume_state"] == "resumable"


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
