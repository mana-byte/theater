"""The card under the tree: the facts about the row in focus the tree has no room for."""

from __future__ import annotations

from dataclasses import replace

from regie.render.card import card_lines
from rich.cells import cell_len

from packages.regie.tests.support.app_fakes import make_participant

NOW = 1_000_000.0


def _lines(participant=None, *, width: int = 48, **kwargs) -> list[str]:
    participant = participant or make_participant("p1", name="Writer", cwd="/srv/proj")
    options = {
        "label_of": lambda pid: {
            "p0": "Boss",
            "p1": "Writer",
            "p2": "Tester",
            "p3": "Reviewer",
        }.get(pid, pid),
        "cost_microcents": None,
        "waiting_on": (),
        "waited_by": (),
        "now": NOW,
        "width": width,
    }
    return [line.plain for line in card_lines(participant, **{**options, **kwargs})]


def test_the_header_names_the_agent_its_harness_and_its_state():
    working = make_participant("p1", name="Writer", status="awaiting_input")
    assert _lines(working)[0] == "Writer  codex  awaiting input"


def test_a_long_directory_keeps_its_end_and_every_line_fits():
    deep = make_participant("p1", name="Writer", cwd="/srv/" + "very-long-segment/" * 6 + "api")
    lines = _lines(deep, width=30)

    assert lines[1].startswith("…") and lines[1].endswith("/api")
    assert all(cell_len(line) <= 30 for line in lines)


def test_a_description_wraps_to_two_lines_and_says_when_there_is_more():
    story = make_participant("p1", name="Writer", description="word " * 40)
    lines = _lines(story, width=24)

    described = [line for line in lines if line.startswith("word")]
    assert len(described) == 2 and described[-1].endswith("…")


def test_cost_and_age_share_a_line_and_vanish_when_unknown():
    young = replace(make_participant("p1", name="Writer", cwd="/srv/proj"), created_at=NOW - 7980)

    assert _lines(young, cost_microcents=42_000_000)[-1] == "$0.42 · up 2h 13m"
    assert _lines(young)[-1] == "up 2h 13m"
    assert _lines(make_participant("p1", name="Writer", cwd="/srv/proj")) == [
        "Writer  codex  idle",
        "/srv/proj",
    ]


def test_relations_name_the_peers_by_their_labels():
    child = make_participant("p1", name="Writer", cwd="/srv/proj", parent_id="p0")
    lines = _lines(child, waiting_on=("p2", "p3"), waited_by=("p3",))

    assert "under Boss" in lines
    assert lines[-2].endswith("waiting on Tester, Reviewer")
    assert lines[-1].endswith("waited on by Reviewer")
