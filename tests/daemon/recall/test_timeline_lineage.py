"""Timeline points report the parent of the session that edited them."""

from __future__ import annotations

import pytest

from tests.support.recall_rows import add_job, add_participant, add_touch_row, setup_repo
from theater.daemon.recall import (
    recall,
)


@pytest.mark.parametrize("handle", ["kid", "kid#4", "unrelated#9"])
def test_point_reports_the_parent_of_the_editing_session(store, tmp_path, handle):
    """A child's edit names the agent that spawned it.

    The question recall answers is "who touched this file"; the follow-up
    is "on whose orders". ``parent_id`` is the lineage half of that and
    comes free from the participants join already in the query.
    """
    root = setup_repo(tmp_path)
    add_participant(store, pid="boss", cwd=root)
    child = add_participant(store, pid="kid", cwd=root, parent_id="boss")
    add_job(store, handle=handle, target_id=child.id)
    add_touch_row(store, job_handle=handle, path="f.py", sha_before="aaa", sha_after="bbb")

    point = recall(store, paths=["f.py"], caller_cwd=root)["f.py"]["timeline"][0]
    assert point["parent_id"] == "boss"
    assert point["participant_id"] == child.id
    assert point["segment"] == handle


def test_root_authored_point_reports_no_parent(store, tmp_path):
    """A participant nobody spawned reports ``parent_id`` as None.

    Explicitly None rather than absent: the caller can tell "this was a
    root" from "this recall build predates lineage".
    """
    root = setup_repo(tmp_path)
    p = add_participant(store, pid="solo", cwd=root)
    add_job(store, handle="h1", target_id=p.id)
    add_touch_row(store, job_handle="h1", path="f.py", sha_before="aaa", sha_after="bbb")

    point = recall(store, paths=["f.py"], caller_cwd=root)["f.py"]["timeline"][0]
    assert point["parent_id"] is None


def test_caller_id_can_differ_from_parent_id(store, tmp_path):
    """Who ordered the job is not always who spawned the agent.

    A ``send`` from a sibling produces a job whose caller is not the
    target's parent. Collapsing the two into one field would report the
    wrong agent for every mid-session prompt.
    """
    root = setup_repo(tmp_path)
    child = add_participant(store, pid="kid", cwd=root, parent_id="boss")
    add_job(store, handle="h1", target_id=child.id, caller_id="sibling")
    add_touch_row(store, job_handle="h1", path="f.py", sha_before="aaa", sha_after="bbb")

    point = recall(store, paths=["f.py"], caller_cwd=root)["f.py"]["timeline"][0]
    assert point["caller_id"] == "sibling"
    assert point["parent_id"] == "boss"


def test_gap_point_carries_no_lineage(store, tmp_path):
    """A gap belongs to no job, so it has no parent and no caller.

    Gap points are built from a broken hash chain, not from a
    participant row. Inventing lineage for them would attribute an
    unclaimed edit to whichever job happens to sit next to it.
    """
    root = setup_repo(tmp_path)
    p = add_participant(store, pid="kid", cwd=root, parent_id="boss")
    add_job(store, handle="old", target_id=p.id, finished_at=1000.0)
    add_touch_row(store, job_handle="old", path="f.py", sha_before="aaa", sha_after="bbb")
    add_job(store, handle="new", target_id=p.id, finished_at=2000.0)
    add_touch_row(store, job_handle="new", path="f.py", sha_before="ccc", sha_after="ddd")

    timeline = recall(store, paths=["f.py"], caller_cwd=root)["f.py"]["timeline"]
    gap = timeline[1]
    assert gap["gap"] is True
    assert "parent_id" not in gap
    assert "caller_id" not in gap
    assert "participant_id" not in gap


def test_attach_parent_names_resolves_known_parents(registry):
    """Runtime names are absent for gaps and null after an identity stops being live."""
    import types

    from theater.daemon.methods import _attach_parent_names

    boss = registry.register(harness="vibe", pane=None, cwd="/tmp/repo")
    retired = registry.register(harness="vibe", pane=None, cwd="/tmp/retired")
    registry.mark_dead(retired.id)
    result = {
        "f.py": {
            "timeline": [
                {"parent_id": boss.id, "participant_id": boss.id},
                {"parent_id": "ghost", "participant_id": "ghost"},
                {"parent_id": None, "participant_id": retired.id},
                {"parent_id": None, "participant_id": boss.id},
                {"gap": True},
            ]
        }
    }
    _attach_parent_names(types.SimpleNamespace(registry=registry), result)

    points = result["f.py"]["timeline"]
    expected = registry.get(boss.id).name
    assert expected
    assert points[0]["parent_name"] == points[0]["name"] == expected
    assert points[1]["parent_name"] is points[1]["name"] is None
    assert points[2]["name"] is None
    assert "parent_name" not in points[2]
    assert points[3]["name"] == expected
    assert points[4] == {"gap": True}
