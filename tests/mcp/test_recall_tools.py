"""The recall and recall_read tool bodies."""

from __future__ import annotations

from tests.support.mcp_tools import resolved
from theater.mcp import tools


async def test_recall_passes_paths_and_depth_to_the_daemon():
    """The daemon does the query; the tool body just forwards the args."""
    s = resolved(recall={"src/main.py": {"timeline": []}})
    await tools.recall(s, paths=["src/main.py"], depth=3)
    p = s.client.params("recall")
    assert p["paths"] == ["src/main.py"]
    assert p["depth"] == 3
    assert p["caller_cwd"]  # the tool sends its own cwd


async def test_recall_omits_handle_when_segment_already_identifies_the_point():
    point = {
        "handle": "p-you#4",
        "segment": "p-you#4",
        "outcome": "done",
        "participant_id": "p-you",
        "name": "Builder",
    }
    s = resolved(recall={"x.py": {"timeline": [point]}})

    result = await tools.recall(s, paths=["x.py"])

    assert result["x.py"]["timeline"] == [
        {
            "segment": "p-you#4",
            "outcome": "done",
            "participant_id": "p-you",
            "name": "Builder",
        }
    ]


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
