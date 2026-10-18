"""Source, attach, drain and a canonical tool turn over the OpenCode 1.x storage recorder."""

from __future__ import annotations

import asyncio

from tests.support.shipped import OpenCodeObserver


def source_for(rec, workdir, **kwargs):
    return OpenCodeObserver(db=rec.path).open_source(cwd=str(workdir), **kwargs)


def attach(src):
    """Stage and accept the initial attachment, as the observer does."""
    batch = asyncio.run(src.read())
    assert batch.attached is not None
    src.commit_attachment()
    return batch


def a_turn_with_a_tool(rec) -> None:
    """The canonical trace: ask, call a tool, then answer in a second step."""
    user = rec.message("msg_u1", "user")
    rec.user_text(user["id"], "prt_u1", "read the note")
    first = rec.message("msg_a1", "assistant")
    rec.text("msg_a1", "prt_t1", "")
    rec.tool("msg_a1", "prt_tool", "call_1", "read", "the secret is pamplemousse")
    rec.step_finish("msg_a1", "prt_sf1", "tool-calls")
    rec.finish(first, "tool-calls")
    second = rec.message("msg_a2", "assistant")
    rec.text("msg_a2", "prt_t2", "pamplemousse")
    rec.step_finish("msg_a2", "prt_sf2", "stop")
    rec.finish(second, "stop")


def drain(rec, workdir):
    """Attach before anything happened, then read the whole session live."""
    src = source_for(rec, workdir)
    attach(src)
    return src
