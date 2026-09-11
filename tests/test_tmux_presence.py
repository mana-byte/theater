"""tmux presence layer: parsing, epoch bracketing, hook ownership."""

from __future__ import annotations

import pytest

import theater.tmux.presence as presence_mod
from theater.tmux.command import TmuxError
from theater.tmux.presence import (
    FocusEventsStatus,
    FocusInventory,
    observe_focus_inventory,
    parse_focus_inventory,
    wake_command,
)

# Captured before the autouse `fake_tmux` fixture swaps the module seams, so
# these tests can restore the real implementations per-test.
_REAL_ENSURE = presence_mod.ensure_focus_events
_REAL_INSTALL = presence_mod.install_focus_wake_hooks
_REAL_REMOVE = presence_mod.remove_focus_wake_hooks
_REAL_HUMAN_PRESENT = presence_mod.human_present
_REAL_OBSERVE = presence_mod.observe_focus_inventory

SEP = "␞"
IDENT = "/tmp/sock\t101\t1"


def pane_line(pane="%1", window="@0", pane_pid="1001", identity=IDENT):
    return f"{pane}{SEP}{window}{SEP}{pane_pid}{SEP}{identity}"


def client_line(
    tty="/dev/ttys001",
    pid="501",
    created="1789162985",
    session="main",
    session_id="$0",
    session_created="1789162980",
    flags="attached,focused,UTF-8",
    readonly="0",
    control="0",
    window="@0",
    active_pane="%1",
    features="focus,RGB",
):
    return SEP.join(
        (
            tty,
            pid,
            created,
            session,
            session_id,
            session_created,
            flags,
            readonly,
            control,
            window,
            active_pane,
            features,
        )
    )


# ---- parse strictness --------------------------------------------------


def test_parse_builds_clients_panes_and_pids():
    inventory = parse_focus_inventory(
        f"{pane_line('%1', '@0')}\n{pane_line('%2', '@1', '1002')}\n",
        f"{client_line()}\n{client_line(tty='/dev/ttys002', pid='502', flags='attached,UTF-8')}\n",
        observed_at=10.0,
    )
    assert isinstance(inventory, FocusInventory)
    assert inventory.panes == {"%1": "@0", "%2": "@1"}
    assert inventory.pane_pids == {"%1": "1001", "%2": "1002"}
    assert inventory.observed_at == 10.0
    assert inventory.server_identity == '["/tmp/sock","101","1"]'
    focused, blurred = inventory.clients
    assert focused.focused and focused.input_capable and focused.focus_reporting
    assert not blurred.focused
    assert focused.identity == ("/dev/ttys001", "501", "1789162985", "$0", "1789162980")
    assert focused.session == "main"
    assert inventory.focus_events_enabled is True


def test_parse_no_clients_is_explicit_absence_not_an_error():
    inventory = parse_focus_inventory(f"{pane_line()}\n", "", observed_at=1.0)
    assert inventory.clients == ()
    assert inventory.panes == {"%1": "@0"}


def test_parse_rejects_malformed_pane_row():
    with pytest.raises(TmuxError):
        parse_focus_inventory(f"%1{SEP}@0{SEP}{SEP}{IDENT}\n", "", observed_at=1.0)
    with pytest.raises(TmuxError):
        parse_focus_inventory(f"%1{SEP}@0{SEP}1001\n", "", observed_at=1.0)


def test_parse_rejects_mixed_server_identities():
    other = "/tmp/sock\t999\t2"
    rows = f"{pane_line()}\n{pane_line('%2', '@1', '1002', other)}\n"
    with pytest.raises(TmuxError):
        parse_focus_inventory(rows, "", observed_at=1.0)


def test_parse_rejects_malformed_client_row():
    with pytest.raises(TmuxError):
        parse_focus_inventory(f"{pane_line()}\n", f"/dev/ttys001{SEP}501\n", observed_at=1.0)
    with pytest.raises(TmuxError):
        parse_focus_inventory(
            f"{pane_line()}\n",
            f"{client_line()}{SEP}extra\n",
            observed_at=1.0,
        )


def test_readonly_and_control_clients_are_not_input_capable():
    inventory = parse_focus_inventory(
        f"{pane_line()}\n",
        f"{client_line(readonly='1')}\n{client_line(control='1', tty='/dev/ttys002', pid='2')}\n",
        observed_at=1.0,
    )
    assert [c.input_capable for c in inventory.clients] == [False, False]


def test_blank_terminal_fields_do_not_poison_unrelated_presence():
    inventory = parse_focus_inventory(
        pane_line(),
        "\n".join(
            [
                client_line(features=""),
                client_line(control="1", tty="", window="", active_pane="", features=""),
            ]
        ),
        observed_at=1.0,
    )
    assert not inventory.clients[0].focus_reporting
    assert not inventory.clients[1].input_capable


async def test_observe_rejects_pane_topology_changed_during_client_read(monkeypatch):
    pane_reads = 0

    async def fake_run(*args, check=True):
        nonlocal pane_reads
        if args[0] == "list-clients":
            return client_line()
        if args[0] == "show-options":
            return "on"
        if args[-1] == presence_mod._IDENTITY:
            return IDENT
        pane_reads += 1
        return pane_line(window="@0" if pane_reads == 1 else "@1")

    monkeypatch.setattr(presence_mod, "run", fake_run)
    with pytest.raises(TmuxError):
        await _REAL_OBSERVE()


# ---- epoch bracketing --------------------------------------------------


async def test_observe_fails_closed_on_mid_query_restart(monkeypatch):
    """A restart between the queries must void the whole observation."""
    monkeypatch.setattr(presence_mod, "observe_focus_inventory", _REAL_OBSERVE)
    outputs = [
        (f"{pane_line()}\n", 0),
        ("", 0),
        ("on", 0),
        (pane_line(identity="/tmp/sock\t999\t2") + "\n", 0),
    ]

    async def fake_run(*args, check=True):
        out, _ = outputs.pop(0)
        return out

    monkeypatch.setattr(presence_mod, "run", fake_run)
    with pytest.raises(TmuxError):
        await observe_focus_inventory()


async def test_observe_accepts_one_verified_epoch(monkeypatch):
    monkeypatch.setattr(presence_mod, "observe_focus_inventory", _REAL_OBSERVE)
    outputs = [(f"{pane_line()}\n", 0), ("", 0), ("on", 0), (pane_line() + "\n", 0)]

    async def fake_run(*args, check=True):
        out, _ = outputs.pop(0)
        return out

    monkeypatch.setattr(presence_mod, "run", fake_run)
    inventory = await observe_focus_inventory(clock=lambda: 5.0)
    assert inventory.server_identity == '["/tmp/sock","101","1"]'
    assert inventory.observed_at == 5.0
    assert inventory.focus_events_enabled is True


async def test_observe_reads_the_option_on_every_pass(monkeypatch):
    """An off option rides the observation: admission never trusts a stale arm."""
    monkeypatch.setattr(presence_mod, "observe_focus_inventory", _REAL_OBSERVE)
    outputs = [(f"{pane_line()}\n", 0), ("", 0), ("off", 0), (pane_line() + "\n", 0)]

    async def fake_run(*args, check=True):
        out, _ = outputs.pop(0)
        return out

    monkeypatch.setattr(presence_mod, "run", fake_run)
    inventory = await observe_focus_inventory(clock=lambda: 5.0)
    assert inventory.focus_events_enabled is False


# ---- copy-mode compatibility -------------------------------------------


async def test_human_present_reads_copy_mode(monkeypatch):
    monkeypatch.setattr(presence_mod, "human_present", _REAL_HUMAN_PRESENT)

    captured = {}

    async def fake_run(*args, check=True):
        captured["args"] = args
        return "1"

    monkeypatch.setattr(presence_mod, "run", fake_run)
    assert await presence_mod.human_present("%3") is True
    assert captured["args"] == ("display-message", "-p", "-t", "%3", "#{pane_in_mode}")

    async def fake_run_zero(*args, check=True):
        return "0"

    monkeypatch.setattr(presence_mod, "run", fake_run_zero)
    assert await presence_mod.human_present("%3") is False


async def test_human_present_propagates_query_errors(monkeypatch):
    """A failed pane query must raise, never read as 'no human'."""
    monkeypatch.setattr(presence_mod, "human_present", _REAL_HUMAN_PRESENT)

    async def failing_run(*args, check=True):
        raise TmuxError("no such pane")

    monkeypatch.setattr(presence_mod, "run", failing_run)
    with pytest.raises(TmuxError):
        await presence_mod.human_present("%3")


# ---- focus events and hook ownership ------------------------------------


class HookStore:
    """A scripted tmux: real hook array semantics, recorded set-hook calls."""

    def __init__(self, hooks=None, sessions=("main",), options=None, clients=""):
        # scope -> event -> {index: command}
        self.hooks = dict(hooks or {})
        self.sessions = list(sessions)
        self.options = dict(options or {})
        self.set_calls: list[tuple[str, ...]] = []
        self.clients = clients

    async def run(self, *args, check=True):
        head = args[0]
        if head == "show-hooks":
            if args[1] == "-g":
                scope, event = ("-g",), args[2]
            else:
                scope, event = (args[1], args[2]), args[3]
            entries = self.hooks.get(scope, {}).get(event, {})
            return "\n".join(f"{event}[{i}] {cmd}" for i, cmd in sorted(entries.items()))
        if head == "set-hook":
            self.set_calls.append(args)
            scope = (args[1],) if args[1] == "-g" else (args[1], args[2])
            tail = args[2:] if scope[0] == "-g" else args[3:]
            if "-u" in tail:
                target = tail[tail.index("-u") + 1]
                event, _, index = target[:-1].partition("[")
                self.hooks.setdefault(scope, {}).setdefault(event, {}).pop(int(index), None)
                return ""
            target, command = tail[0], tail[1]
            event, _, index = target[:-1].partition("[")
            self.hooks.setdefault(scope, {}).setdefault(event, {})[int(index)] = command
            return ""
        if head == "show-options":
            return self.options.get(args[-1], "off")
        if head == "set-option":
            self.options[args[-2]] = args[-1]
            return ""
        if head == "list-sessions":
            return "\n".join(self.sessions)
        if head == "list-clients":
            return self.clients
        raise AssertionError(f"unexpected tmux call: {args}")


_RESTORE = {
    "install_focus_wake_hooks": _REAL_INSTALL,
    "remove_focus_wake_hooks": _REAL_REMOVE,
    "ensure_focus_events": _REAL_ENSURE,
}


def wire_store(monkeypatch, store, restore=()):
    monkeypatch.setattr(presence_mod, "run", store.run)
    for name in restore:
        monkeypatch.setattr(presence_mod, name, _RESTORE[name])


async def test_ensure_focus_events_enables_and_diagnoses(monkeypatch):
    store = HookStore(options={"focus-events": "off"}, clients="/dev/ttys001␞vt100-only\n")
    wire_store(monkeypatch, store)
    monkeypatch.setattr(presence_mod, "ensure_focus_events", _REAL_ENSURE)

    status = await presence_mod.ensure_focus_events()
    assert isinstance(status, FocusEventsStatus)
    assert status.enabled is True  # verified by re-read, not assumed
    assert status.previously_off is True
    assert status.focusless_clients == ("/dev/ttys001",)
    assert store.options["focus-events"] == "on"


async def test_install_appends_without_touching_user_entries(monkeypatch):
    ours = wake_command("chan")
    user_hooks = {("-g",): {"client-focus-in": {0: "run-shell true"}}}
    store = HookStore(hooks=user_hooks)
    wire_store(monkeypatch, store, restore=("install_focus_wake_hooks",))

    await presence_mod.install_focus_wake_hooks("chan")
    entries = store.hooks[("-g",)]["client-focus-in"]
    assert entries[0] == "run-shell true"
    assert entries[1] == ours
    assert len([cmd for cmd in entries.values() if cmd == ours]) == 1


async def test_install_sweeps_stale_entries_from_previous_runs(monkeypatch):
    ours = wake_command("chan")
    store = HookStore(hooks={("-g",): {"client-focus-in": {0: ours, 1: ours}}})
    wire_store(monkeypatch, store, restore=("install_focus_wake_hooks",))

    await presence_mod.install_focus_wake_hooks("chan")
    entries = store.hooks[("-g",)]["client-focus-in"]
    assert list(entries.values()) == [ours]


async def test_install_covers_session_local_shadowing(monkeypatch):
    """A session-local hook array shadows the global one, so cover both."""
    ours = wake_command("chan")
    local = {("-t", "main:"): {"client-focus-in": {0: "run-shell local-thing"}}}
    store = HookStore(hooks=local)
    wire_store(monkeypatch, store, restore=("install_focus_wake_hooks",))

    await presence_mod.install_focus_wake_hooks("chan")
    global_entries = store.hooks[("-g",)]["client-focus-in"]
    local_entries = store.hooks[("-t", "main:")]["client-focus-in"]
    assert ours in global_entries.values()
    assert local_entries[0] == "run-shell local-thing"
    assert ours in local_entries.values()


async def test_remove_sweeps_only_owned_entries(monkeypatch):
    ours = wake_command("chan")
    store = HookStore(
        hooks={
            ("-g",): {"client-focus-in": {0: "run-shell true", 1: ours}},
            ("-t", "main:"): {"client-focus-in": {0: ours}},
        }
    )
    wire_store(monkeypatch, store, restore=("remove_focus_wake_hooks",))

    await presence_mod.remove_focus_wake_hooks("chan")
    assert store.hooks[("-g",)]["client-focus-in"] == {0: "run-shell true"}
    assert store.hooks[("-t", "main:")]["client-focus-in"] == {}
