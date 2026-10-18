"""Write a Codex rollout file and hold a process open for correlation tests."""

from __future__ import annotations

import json
from pathlib import Path

from theater import proc


def write_rollout(
    root: Path,
    session_id: str,
    cwd: Path,
    *,
    at: str,
    text: str,
    metadata: dict[str, object] | None = None,
) -> Path:
    """One codex rollout, filed the way codex files them.

    `~/.codex/sessions/<YYYY>/<MM>/<DD>/rollout-<local ISO>-<session id>.jsonl`,
    opening with the `session_meta` record that carries the cwd.
    """
    day = root / "2026" / "08" / "17"
    day.mkdir(parents=True, exist_ok=True)
    path = day / f"rollout-2026-08-17T{at}-{session_id}.jsonl"
    path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "timestamp": "2026-08-17T01:19:02.000Z",
                        "type": "session_meta",
                        "payload": {"id": session_id, "cwd": str(cwd), **(metadata or {})},
                    }
                ),
                json.dumps(
                    {
                        "timestamp": "2026-08-17T01:19:03.000Z",
                        "type": "event_msg",
                        "payload": {"type": "agent_message", "message": text},
                    }
                ),
            ]
        )
        + "\n"
    )
    return path


class Asked:
    """Which pids each `theater.proc` helper was asked about, in order.

    One list per helper rather than one between them: "the operating system was
    never asked" and "the process was identified but never opened" are
    different claims, and a recorder that pooled them could not tell a test
    which of the two it had proved.
    """

    def __init__(self) -> None:
        self.comm: list[int] = []
        self.open_files: list[int] = []
        self.descendants: list[int] = []

    @property
    def anything(self) -> list[int]:
        return self.comm + self.open_files + self.descendants


def hold(
    monkeypatch,
    held: dict[int, list[Path]],
    *,
    comm: str = "codex",
    comms: dict[int, str] | None = None,
    beneath: list[tuple[int, str]] | None = None,
) -> Asked:
    """Pretend each pid in *held* is a codex process holding those files open.

    *comm* is what `ps` reports for the pane process; the default is the shape
    of a pane Theater spawned, where the pane process is the CLI. Pass *comms*
    to give different pids different commands, and *beneath* to put processes
    under the pane — which nothing should ever ask about, and which is stubbed
    here precisely so that a version which did would be caught.

    *beneath* is `(pid, comm)` pairs because that is what `proc.descendants`
    returns. A stub of a different shape would make a version that searched the
    tree fail on the shape rather than on the attribution, which is the one
    thing these tests must not mistake for a pass.

    Returns the record of what the probe asked the operating system, so a test
    can assert that a participant with no pid, or one whose answer was already
    exact, never reaches it at all.
    """
    asked = Asked()

    def process_comm(pid: int) -> str:
        asked.comm.append(pid)
        if comms is not None:
            return comms.get(pid, "")
        return comm

    def open_files(pid: int) -> list[Path]:
        asked.open_files.append(pid)
        return list(held.get(pid, ()))

    def descendants(pid: int) -> list[tuple[int, str]]:
        asked.descendants.append(pid)
        return list(beneath or ())

    monkeypatch.setattr(proc, "comm", process_comm)
    monkeypatch.setattr(proc, "open_files", open_files)
    monkeypatch.setattr(proc, "descendants", descendants)
    return asked
