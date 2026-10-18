"""`theater bus`: event rendering, kind filter and the follow cursor."""

from __future__ import annotations

import json

import pytest

from tests.support.cli_fakes import FollowClient, StopFollow, parse
from theater import cli
from theater.cli.commands import bus as bus_mod
from theater.cli.commands import participants as participants_mod
from theater.formatting import event_summary


def event(**kw):
    row = {"id": 1, "ts": 0, "from_id": None, "to_id": None, "kind": "x", "payload": None}
    row.update(kw)
    return row


def test_a_tool_call_shows_the_tool_name():
    line = cli._bus_line(
        event(kind="agent.tool_call", from_id="p-abc123", payload={"tool": "bash"}),
        200,
    )
    assert "agent.tool_call" in line
    assert "p-abc123" in line
    assert "[bash]" in line


def test_assistant_text_is_flattened_to_one_line():
    line = cli._bus_line(event(kind="agent.assistant", payload={"text": "one\ntwo   three"}), 200)
    assert "one two three" in line
    assert "\n" not in line


def test_a_turn_boundary_is_visible():
    assert "(turn end)" in cli._bus_line(event(payload={"text": "done", "turn_end": True}), 200)


def test_an_unrecognised_payload_is_shown_rather_than_dropped():
    line = cli._bus_line(event(kind="participant.joined", payload={"pane": "%9"}), 200)
    assert "%9" in line


def test_bookkeeping_fields_alone_do_not_produce_noise():
    """ts/index/turn_end are chrome; with nothing else there is nothing to say."""
    assert event_summary({"ts": None, "index": 4, "turn_end": False}) == ""


def test_a_long_line_is_clipped_to_the_terminal():
    line = cli._bus_line(event(payload={"text": "x" * 500}), 80)
    assert len(line) < 80


def test_a_directed_event_shows_both_ends():
    line = cli._bus_line(event(from_id="p-aaa", to_id="p-bbb"), 200)
    assert "p-aaa -> p-bbb" in line


def test_the_kind_filter_is_a_prefix():
    rows = [event(kind="agent.user"), event(kind="participant.joined")]
    assert [r["kind"] for r in cli._matching(rows, "agent.")] == ["agent.user"]
    assert cli._matching(rows, None) == rows


def test_json_output_is_one_object_per_line(capsys):
    args = parse("bus", "--json")
    cli._emit_bus([event(id=1), event(id=2)], args)
    lines = capsys.readouterr().out.strip().split("\n")
    assert [json.loads(line)["id"] for line in lines] == [1, 2]


class FakeClient:
    """Replays canned bus.tail responses, recording the cursors asked for."""

    def __init__(self, pages):
        self.pages = list(pages)
        self.cursors: list[int] = []

    async def call(self, method, **params):
        assert method == "bus.tail"
        self.cursors.append(params.get("after_id", 0))
        if not self.pages:
            raise KeyboardInterrupt
        return self.pages.pop(0)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None


@pytest.fixture
def fake_follow(monkeypatch):
    def install(pages):
        client = FakeClient(pages)
        monkeypatch.setattr(bus_mod, "DaemonClient", lambda *a, **k: client)
        monkeypatch.setattr(participants_mod, "DaemonClient", lambda *a, **k: client)
        return client

    return install


async def _run_follow(args):
    with pytest.raises(KeyboardInterrupt):
        await cli._follow_bus(args)


async def test_follow_advances_the_cursor_past_what_it_printed(fake_follow, capsys):
    client = fake_follow([[event(id=7, kind="agent.user")], [event(id=9, kind="agent.user")]])
    args = parse("bus", "-f", "--interval", "0")
    await _run_follow(args)
    assert client.cursors == [0, 7, 9]
    assert capsys.readouterr().out.count("agent.user") == 2


async def test_follow_from_an_empty_bus_starts_at_zero(fake_follow):
    client = fake_follow([[], [event(id=1)]])
    args = parse("bus", "-f", "--interval", "0")
    await _run_follow(args)
    assert client.cursors == [0, 0, 1]


def test_bus_with_no_events_says_so(answers, capsys):
    answers["replies"] = {"bus.tail": []}
    assert cli.cmd_bus(parse("bus")) == 0
    assert "no events" in capsys.readouterr().out


def test_bus_filters_by_kind_prefix(answers, capsys):
    answers["replies"] = {
        "bus.tail": [
            {
                "id": 1,
                "ts": 0,
                "kind": "agent.assistant",
                "from_id": "a",
                "to_id": None,
                "payload": {"text": "hi"},
            },
            {"id": 2, "ts": 0, "kind": "job.created", "from_id": "a", "to_id": "b", "payload": {}},
        ]
    }
    assert cli.cmd_bus(parse("bus", "--kind", "agent")) == 0
    out = capsys.readouterr().out
    assert "agent.assistant" in out
    assert "job.created" not in out


def test_follow_says_when_the_feed_fell_behind(monkeypatch, capsys):
    """A burst larger than one batch drops the middle; silence would look complete."""
    client = FollowClient(
        [
            [{"id": 1, "ts": 0, "kind": "agent.user", "actor_id": "p-a", "payload": {}}],
            [{"id": 5, "ts": 0, "kind": "agent.user", "actor_id": "p-a", "payload": {}}],
        ]
    )
    monkeypatch.setattr(bus_mod, "DaemonClient", lambda: client)
    with pytest.raises(StopFollow):
        cli.cmd_bus(parse("bus", "-f", "--interval", "0"))
    assert "3 events dropped" in capsys.readouterr().out


def test_follow_holds_its_cursor_across_an_empty_poll(monkeypatch, capsys):
    """Advancing on nothing would skip whatever the daemon writes next."""
    client = FollowClient(
        [
            [{"id": 7, "ts": 0, "kind": "agent.user", "actor_id": "p-a", "payload": {}}],
            [],
        ]
    )
    monkeypatch.setattr(bus_mod, "DaemonClient", lambda: client)
    with pytest.raises(StopFollow):
        cli.cmd_bus(parse("bus", "-f", "--interval", "0"))
    assert [p.get("after_id") for _, p in client.calls] == [None, 7, 7]


def test_read_at_is_omitted_from_event_display():
    assert event_summary({"ts": None, "index": 4, "observed_at": 1.0, "read_at": 2.0}) == ""
