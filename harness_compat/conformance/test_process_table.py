"""The survival scenario's process evidence reads the table and names what outlived a restart."""

from __future__ import annotations

import os

from harness_compat.support import process_table
from harness_compat.support.process_table import Process

_PS = """\
    1     0     1 Sat Oct 24 11:00:00 2026
  100     1   100 Sat Oct 24 13:14:44 2026
  200   100   200 Sat Oct 24 13:14:45 2026
  300   100   100 Sat Oct 24 13:14:46 2026
  400   999   400 Sat Oct 24  9:01:02 2026
"""


def test_the_start_time_is_the_rest_of_the_line_whatever_its_width() -> None:
    parsed = process_table.parse(_PS)

    assert parsed[400] == Process(400, 999, 400, "Sat Oct 24  9:01:02 2026")
    assert set(parsed) == {1, 100, 200, 300, 400}


def test_the_table_lists_this_process_with_its_parent() -> None:
    mine = process_table.table()[os.getpid()]

    assert mine.ppid == os.getppid()


def test_only_children_that_lead_their_own_group_count_as_detached() -> None:
    parsed = process_table.parse(_PS)

    # 200 is a session of its own; 300 is a helper in its parent's group; 400 has another parent.
    assert [p.pid for p in process_table.detached_children(parsed, 100)] == [200]


def test_a_process_is_lost_when_it_exited_or_its_pid_was_recycled() -> None:
    recorded = process_table.parse(_PS)
    later = dict(recorded)
    del later[200]
    later[300] = Process(300, 1, 300, "Sat Oct 24 13:20:00 2026")
    later[100] = Process(100, 7, 100, "Sat Oct 24 13:14:44 2026")  # reparented, still the same

    assert [p.pid for p in process_table.lost(recorded.values(), later)] == [200, 300]
