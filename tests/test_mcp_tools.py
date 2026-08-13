"""The tool bodies an agent reaches through MCP.

The framework wrapper is covered in test_mcp_server.py. What can be wrong here
is quieter: a tool that forgets to identify itself first is filed as External
and becomes unaddressable, and one that forgets to name its caller defeats the
deadlock rail — neither shows up as an error, only as work that never lands.
"""

from __future__ import annotations

import pytest

from theater.mcp import tools

RECORD = {
    "id": "p-me",
    "harness": "vibe",
    "tier": "spawned",
    "status": "idle",
    "cwd": "/tmp/project",
    "branch": None,
    "parent_id": None,
    "addressable": True,
    "tmux_pane": "%3",
    "pid": 4321,
}


class FakeClient:
    """A daemon that answers from a dict and remembers what it was asked."""

    def __init__(self, **replies):
        self.replies = replies
        self.calls: list[tuple[str, dict]] = []
        #: Set these to make a specific method raise instead of answering.
        self._raise_on: str | None = None
        self._raise_exc: Exception | None = None

    async def call(self, method, **params):
        self.calls.append((method, params))
        if method == self._raise_on and self._raise_exc is not None:
            raise self._raise_exc
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
        "harness": "vibe",
        "tier": "spawned",
        "status": "idle",
        "cwd": "/tmp/project",
        "branch": None,
        "parent_id": None,
        "addressable": True,
    }
    assert "pid" not in got, "process detail is noise to another agent"


async def test_list_participants_marks_which_row_is_the_caller():
    """Without this an agent can send a prompt to itself and wait on its own turn."""
    rows = [{**RECORD, "id": "p-me"}, {**RECORD, "id": "p-other"}]
    got = await tools.list_participants(resolved(**{"participants.list": rows}))
    assert [r["is_self"] for r in got] == [True, False]


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


async def test_spawn_names_the_caller_as_the_parent():
    """Lineage is set here or never — the child cannot know who asked for it."""
    s = resolved()
    await tools.spawn_session(s, harness="vibe", prompt="hi", approval="manual")
    assert s.client.params("spawn")["parent_id"] == "p-me"


async def test_spawn_accepts_no_prompt():
    """No prompt means the daemon starts a plain CLI and resolves the job."""
    s = resolved()
    await tools.spawn_session(s, harness="vibe", approval="manual")
    assert s.client.params("spawn")["prompt"] is None


async def test_spawn_defaults_the_cwd_to_this_process():
    s = resolved()
    await tools.spawn_session(s, harness="vibe", prompt="hi", approval="manual")
    assert s.client.params("spawn")["cwd"]


async def test_register_pane_settles_identity_on_the_pane_it_was_told():
    """The MCP env allowlist hides TMUX_PANE, so the agent reads it and tells us."""
    s = session(hello={**RECORD, "id": "p-adopted"})
    await tools.register_pane(s, pane="%12")
    assert s.client.params("hello")["pane"] == "%12"
    assert (s.participant_id, s._resolved) == ("p-adopted", True)


async def test_await_names_the_caller_so_a_deadlock_can_be_refused():
    """Waiting on someone who is waiting on you has to be visible to the daemon."""
    s = resolved(**{"jobs.await": []})
    await tools.await_sessions(s, handles=["h#1"], max_wait=5.0)
    p = s.client.params("jobs.await")
    assert (p["caller_id"], p["handles"], p["max_wait"]) == ("p-me", ["h#1"], 5.0)


async def test_send_names_the_caller_so_the_reply_comes_back():
    s = resolved(send={"handle": "p-you#1"})
    await tools.send_prompt(s, target_id="p-you", prompt="hello")
    p = s.client.params("send")
    assert (p["target"], p["prompt"], p["caller_id"]) == ("p-you", "hello", "p-me")


async def test_send_identifies_first_or_the_reply_has_nowhere_to_go():
    s = session(send={"handle": "p-you#1"})
    await tools.send_prompt(s, target_id="p-you", prompt="hello")
    assert s.client.methods == ["hello", "send"]


async def test_read_transcript_asks_for_the_number_of_events_requested():
    s = resolved(read_transcript={"id": "p-you", "events": []})
    await tools.read_transcript(s, target_id="p-you", last_n=12)
    assert s.client.params("read_transcript") == {"id": "p-you", "last_n": 12}


async def test_put_child_back_in_the_wound_kills_a_direct_child():
    """The happy path: a child whose parent_id matches the caller gets killed."""
    child = {**RECORD, "id": "p-child", "parent_id": "p-me", "status": "idle"}
    s = resolved(
        **{
            "participants.get": child,
            "participant.kill": {"id": "p-child", "killed": True},
        }
    )
    result = await tools.put_child_back_in_the_wound(s, target_id="p-child")
    assert result == {"id": "p-child", "killed": True}
    assert s.client.params("participant.kill") == {"id": "p-child"}


async def test_put_child_back_in_the_wound_refuses_a_non_child():
    """A target whose parent_id is not the caller's is refused, not killed."""
    stranger = {**RECORD, "id": "p-sib", "parent_id": "p-someone-else", "status": "idle"}
    s = resolved(**{"participants.get": stranger, "participant.kill": {}})
    with pytest.raises(tools.KillRefused) as exc:
        await tools.put_child_back_in_the_wound(s, target_id="p-sib")
    assert exc.value.code == "not_your_child"
    #: The daemon's participant.kill must never have been called.
    assert "participant.kill" not in s.client.methods


async def test_put_child_back_in_the_wound_refuses_self():
    """No self-kill: the caller naming its own id is refused before any lookup."""
    s = resolved(**{"participants.get": RECORD, "participant.kill": {}})
    with pytest.raises(tools.KillRefused) as exc:
        await tools.put_child_back_in_the_wound(s, target_id="p-me")
    assert exc.value.code == "no_self_kill"
    assert "participants.get" not in s.client.methods
    assert "participant.kill" not in s.client.methods


async def test_put_child_back_in_the_wound_treats_an_already_dead_child_as_a_no_op():
    """Killing a dead thing is not a failure — say so and do nothing."""
    dead_child = {**RECORD, "id": "p-child", "parent_id": "p-me", "status": "dead"}
    s = resolved(**{"participants.get": dead_child, "participant.kill": {}})
    result = await tools.put_child_back_in_the_wound(s, target_id="p-child")
    assert result == {"id": "p-child", "killed": False, "reason": "already_dead"}
    assert "participant.kill" not in s.client.methods


async def test_put_child_back_in_the_wound_refuses_a_nonexistent_target_as_not_your_child():
    """A target that does not exist is not your child, not a crash."""
    from theater.protocol import RemoteError

    client = FakeClient()
    client._raise_on = "participants.get"
    client._raise_exc = RemoteError("not_found", "no participant 'p-ghost'")
    s = tools.Session(participant_id="p-me", harness="vibe", client=client)
    s._resolved = True
    with pytest.raises(tools.KillRefused) as exc:
        await tools.put_child_back_in_the_wound(s, target_id="p-ghost")
    assert exc.value.code == "not_your_child"
    assert "participant.kill" not in s.client.methods


async def test_put_child_back_in_the_wound_identifies_first():
    """An unresolved session must announce itself before it can name its children."""
    s = session(**{"participants.get": {**RECORD, "parent_id": "p-me"}, "participant.kill": {}})
    await tools.put_child_back_in_the_wound(s, target_id="p-child")
    assert s.client.methods[0] == "hello"
