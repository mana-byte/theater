"""CLI tests for the control commands and the spawn wiring flag.

``call_sync`` is the seam, as in the rest of ``test_cli.py``: these assert what
each command asks the daemon for, what it prints, and its exit codes — the
socket round trip is covered by ``test_control_rpc.py``.
"""

from __future__ import annotations

import json

import pytest

from theater import cli
from theater.cli.commands import controls as controls_mod


def parse(*argv):
    return cli._parser().parse_args(list(argv))


@pytest.fixture
def answers(monkeypatch):
    state = {"replies": {}, "calls": []}

    def call_sync(method, **params):
        state["calls"].append((method, params))
        reply = state["replies"].get(method, [])
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(controls_mod, "call_sync", call_sync)
    monkeypatch.setattr("theater.cli.commands.participants.call_sync", call_sync, raising=False)
    return state


# ---- steer -----------------------------------------------------------------


def test_steer_forwards_the_cli_identity_and_the_optional_job(answers, capsys):
    answers["replies"] = {
        "participant.steer": {"handle": "p-1#3", "state": "running", "prompt": "orig"}
    }
    assert cli.cmd_steer(parse("steer", "p-1", "amend it", "--job", "p-1#3")) == 0
    assert answers["calls"] == [
        (
            "participant.steer",
            {
                "target": "p-1",
                "prompt": "amend it",
                "caller_id": "cli",
                "job_handle": "p-1#3",
            },
        )
    ]
    assert "p-1#3" in capsys.readouterr().out


def test_steer_json_prints_the_job_verbatim(answers, capsys):
    record = {"handle": "p-1#3", "state": "running"}
    answers["replies"] = {"participant.steer": record}
    assert cli.cmd_steer(parse("steer", "p-1", "amend", "--json")) == 0
    assert json.loads(capsys.readouterr().out) == record


def test_steer_accepted_delivery_is_printed_as_confirmed(answers, capsys):
    answers["replies"] = {
        "participant.steer": {
            "handle": "p-1#3",
            "state": "running",
            "delivery": {"operation_id": "op-7", "phase": "settled", "result": "accepted"},
        }
    }
    assert cli.cmd_steer(parse("steer", "p-1", "amend")) == 0
    assert "amended job p-1#3" in capsys.readouterr().out


def test_steer_unknown_delivery_warns_and_never_prints_success(answers, capsys):
    answers["replies"] = {
        "participant.steer": {
            "handle": "p-1#3",
            "state": "running",
            "delivery": {
                "operation_id": "op-7",
                "phase": "settled",
                "result": "unknown",
                "error_code": "delivery_unknown",
                "error": "runtime I/O exploded",
            },
        }
    }
    assert cli.cmd_steer(parse("steer", "p-1", "amend")) == 1
    captured = capsys.readouterr()
    assert "amended job" not in captured.out, "an unknown delivery is not a success line"
    assert "delivery_unknown" in captured.err
    assert "runtime I/O exploded" in captured.err
    assert "keeps running" in captured.err
    assert "p-1#3" in captured.err


def test_steer_unknown_delivery_json_stays_verbatim_but_exits_nonzero(answers, capsys):
    record = {
        "handle": "p-1#3",
        "state": "running",
        "delivery": {"phase": "settled", "result": "unknown", "error_code": "delivery_unknown"},
    }
    answers["replies"] = {"participant.steer": record}
    assert cli.cmd_steer(parse("steer", "p-1", "amend", "--json")) == 1
    assert json.loads(capsys.readouterr().out) == record


def test_steer_without_delivery_metadata_stays_a_confirmed_amendment(answers, capsys):
    """An answer without the additive field keeps the old confirmed reading."""
    answers["replies"] = {"participant.steer": {"handle": "p-1#3", "state": "running"}}
    assert cli.cmd_steer(parse("steer", "p-1", "amend")) == 0
    assert "amended job p-1#3" in capsys.readouterr().out


# ---- queue -----------------------------------------------------------------


def test_queue_creates_and_prints_the_followup_handle(answers, capsys):
    answers["replies"] = {"participant.queue_followup": {"handle": "p-1#4", "state": "running"}}
    assert cli.cmd_queue(parse("queue", "p-1", "do more")) == 0
    assert answers["calls"] == [
        (
            "participant.queue_followup",
            {"target": "p-1", "prompt": "do more", "caller_id": "cli"},
        )
    ]
    out = capsys.readouterr().out
    assert "p-1#4" in out
    assert "queued p-1" in out


def test_queue_json_prints_the_job_verbatim(answers, capsys):
    record = {"handle": "p-1#4", "state": "running"}
    answers["replies"] = {"participant.queue_followup": record}
    assert cli.cmd_queue(parse("queue", "p-1", "do more", "--json")) == 0
    assert json.loads(capsys.readouterr().out) == record


# ---- settings --------------------------------------------------------------


def test_settings_applied_prints_effective_values(answers, capsys):
    answers["replies"] = {
        "participant.settings.update": {
            "id": "p-1",
            "applied": True,
            "model": "gpt-5.6-sol",
            "reasoning_effort": "high",
        }
    }
    assert cli.cmd_settings(parse("settings", "p-1", "--model", "gpt-5.6-sol")) == 0
    assert answers["calls"] == [
        (
            "participant.settings.update",
            {
                "target": "p-1",
                "caller_id": "cli",
                "model": "gpt-5.6-sol",
                "reasoning_effort": None,
            },
        )
    ]
    out = capsys.readouterr().out
    assert "settings applied for p-1" in out
    assert "model=gpt-5.6-sol" in out


def test_settings_rejection_is_an_error_exit_with_the_daemon_reason(answers, capsys):
    answers["replies"] = {
        "participant.settings.update": {
            "id": "p-1",
            "applied": False,
            "model": None,
            "reasoning_effort": None,
            "error_code": "gated_by_backend",
            "error": "the installed native version gates settings",
        }
    }
    assert cli.cmd_settings(parse("settings", "p-1", "--reasoning-effort", "high")) == 1
    err = capsys.readouterr().err
    assert "gated_by_backend" in err
    assert "the installed native version gates settings" in err


def test_settings_uncertain_application_stays_visible(answers, capsys):
    answers["replies"] = {
        "participant.settings.update": {
            "id": "p-1",
            "applied": None,
            "model": "gpt-5.6-sol",
            "reasoning_effort": None,
        }
    }
    assert cli.cmd_settings(parse("settings", "p-1", "--model", "gpt-5.6-sol", "--json")) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["applied"] is None


def test_settings_json_exit_code_follows_the_outcome(answers, capsys):
    """--json still prints the structure, but never fakes success."""
    answers["replies"] = {
        "participant.settings.update": {"id": "p-1", "applied": True, "model": "m"}
    }
    assert cli.cmd_settings(parse("settings", "p-1", "--model", "m", "--json")) == 0


# ---- controls --------------------------------------------------------------


def test_controls_renders_wiring_capabilities_and_queue(answers, capsys):
    answers["replies"] = {
        "participant.controls": {
            "id": "p-1",
            "wiring": "native",
            "backend_generation": 1,
            "native_session_id": "thread-1",
            "health": {"connection": "connected", "diagnostics": []},
            "settings": {"model": "gpt-5.6-sol", "reasoning_effort": "high"},
            "capabilities": {
                "send": {"available": True},
                "steer": {"available": True},
                "queue_followup": {"available": True},
                "settings_update": {
                    "available": False,
                    "reason": "gated_by_backend",
                    "detail": "the backend gates settings",
                },
                "interrupt": {"available": True},
            },
            "active_turn": {"native_turn_id": "turn-2", "job_handle": "p-1#2"},
            "queued": ["p-1#5"],
        }
    }
    assert cli.cmd_controls(parse("controls", "p-1")) == 0
    out = capsys.readouterr().out
    assert "wiring=native" in out
    assert "connection=connected" in out
    assert "model=gpt-5.6-sol" in out
    assert "turn-2" in out
    assert "job=p-1#2" in out
    assert "p-1#5" in out
    assert "settings_update" in out
    assert "unavailable (gated_by_backend)" in out
    assert "the backend gates settings" in out


def test_controls_json_is_the_daemon_answer(answers, capsys):
    record = {"id": "p-1", "wiring": "legacy", "capabilities": {}}
    answers["replies"] = {"participant.controls": record}
    assert cli.cmd_controls(parse("controls", "p-1", "--json")) == 0
    assert json.loads(capsys.readouterr().out) == record


def test_controls_legacy_reasons_render(answers, capsys):
    answers["replies"] = {
        "participant.controls": {
            "id": "p-1",
            "wiring": "legacy",
            "health": None,
            "settings": None,
            "capabilities": {
                "steer": {
                    "available": False,
                    "reason": "wiring_mode",
                    "detail": "steering requires native runtime wiring",
                },
            },
            "active_turn": None,
            "queued": [],
        }
    }
    assert cli.cmd_controls(parse("controls", "p-1")) == 0
    out = capsys.readouterr().out
    assert "settings: fixed at launch" in out
    assert "active turn: none" in out
    assert "queued followups: none" in out
    assert "unavailable (wiring_mode)" in out
    assert "steering requires native runtime wiring" in out


def test_controls_names_pending_human_interaction(answers, capsys):
    answers["replies"] = {
        "participant.controls": {
            "id": "p-1",
            "wiring": "native",
            "health": {"connection": "degraded", "diagnostics": []},
            "capabilities": {},
            "active_turn": {
                "native_turn_id": "turn-3",
                "job_handle": None,
                "pending_interaction": {
                    "kind": "approval",
                    "native_turn_id": "turn-3",
                    "details": "approve the tool call",
                },
            },
            "queued": [],
        }
    }
    assert cli.cmd_controls(parse("controls", "p-1")) == 0
    out = capsys.readouterr().out
    assert "job=human turn" in out
    assert "pending approval" in out
    assert "answer it in the native UI" in out


# ---- interrupt -------------------------------------------------------------


def test_interrupt_prints_the_cancelled_followups(answers, capsys):
    answers["replies"] = {
        "participant.interrupt": {
            "id": "p-1",
            "interrupted": True,
            "cancelled_followups": ["p-1#5", "p-1#6"],
        }
    }
    assert cli.cmd_interrupt(parse("interrupt", "p-1")) == 0
    assert answers["calls"] == [("participant.interrupt", {"target": "p-1", "caller_id": "cli"})]
    assert "interrupted p-1" in capsys.readouterr().out


def test_interrupt_when_idle_prints_the_reason(answers, capsys):
    answers["replies"] = {
        "participant.interrupt": {"id": "p-1", "interrupted": False, "reason": "already_idle"}
    }
    assert cli.cmd_interrupt(parse("interrupt", "p-1", "--json")) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"id": "p-1", "interrupted": False, "reason": "already_idle"}


# ---- spawn wiring ----------------------------------------------------------


def test_spawn_defaults_wiring_to_auto(monkeypatch, answers):
    monkeypatch.setattr(cli.tmux, "current_session_sync", lambda: "main")
    answers["replies"] = {"spawn": {"id": "p-new", "harness": "vibe", "tmux_pane": "%4"}}
    assert cli.cmd_spawn(parse("spawn", "vibe", "hi", "--approval", "manual")) == 0
    assert answers["calls"][0][1]["wiring"] == "auto"


def test_spawn_passes_the_explicit_legacy_opt_out(monkeypatch, answers):
    monkeypatch.setattr(cli.tmux, "current_session_sync", lambda: "main")
    answers["replies"] = {"spawn": {"id": "p-new", "harness": "vibe", "tmux_pane": "%4"}}
    assert (
        cli.cmd_spawn(parse("spawn", "vibe", "hi", "--approval", "manual", "--wiring", "legacy"))
        == 0
    )
    assert answers["calls"][0][1]["wiring"] == "legacy"


def test_spawn_rejects_unknown_wiring_at_parse_time():
    with pytest.raises(SystemExit):
        cli._parser().parse_args(
            ["spawn", "vibe", "hi", "--approval", "manual", "--wiring", "bogus"]
        )
