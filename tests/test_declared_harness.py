"""Harnesses declared in config.toml, and the screen watching they require.

Two halves, and the second is the one that earns the tests. Declaring a harness
is parsing; observing one is a heuristic promoted from a display hint to the
signal that finishes a job. The failure mode there is not a wrong colour in a
listing, it is a caller handed half an answer, so the timing cases below are
the point of this file.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from theater import cli
from theater import config as cfg
from theater import harness as harness_registry
from theater import paths
from theater.daemon.jobs import JobManager
from theater.daemon.observer import IDLE_CONFIRMATIONS, Observer, screen_result
from theater.harness.declared import DeclaredHarness, render
from theater.models import BadRequest, JobState, Status

CODEX = """
[harness.codex]
binary = "codex"
icon = "@"
idle_prompts = ["›"]
approvals = { manual = [], edits = ["--full-auto"], yolo = ["--danger"] }
"""


def write(text: str) -> None:
    paths.config_path().write_text(text, encoding="utf-8")


def declare(body: str) -> cfg.Config:
    write(body)
    return cfg.load()


def spec(**overrides) -> cfg.HarnessSpec:
    base = {
        "binary": "codex",
        "idle_prompts": ["›"],
        "approvals": {"manual": [], "edits": ["--full-auto"], "yolo": ["--danger"]},
    }
    return cfg.HarnessSpec(**{**base, **overrides})


# ---- parsing ------------------------------------------------------------


def test_a_declaration_becomes_a_spec():
    loaded = declare(CODEX)
    assert set(loaded.harnesses) == {"codex"}
    codex = loaded.harnesses["codex"]
    assert codex.binary == "codex"
    assert codex.icon == "@"
    assert codex.approvals["edits"] == ["--full-auto"]
    # Defaulted, not absent: the prompt is the conventional single positional.
    assert codex.argv == ["{prompt}"]


def test_declaring_nothing_leaves_no_harnesses():
    write("[rails]\nbudget = 5\n")
    assert cfg.load().harnesses == {}


@pytest.mark.parametrize(
    "body, expected",
    [
        ('[harness.codex]\nidle_prompts = ["›"]\napprovals = {}\n', "'binary'"),
        ('[harness.codex]\nbinary = "c"\napprovals = {}\n', "'idle_prompts'"),
        ('[harness.codex]\nbinary = "c"\nidle_prompts = ["›"]\n', "'approvals'"),
    ],
)
def test_a_missing_required_key_is_named(body, expected):
    write(body)
    with pytest.raises(cfg.ConfigError) as exc:
        cfg.load()
    assert expected in str(exc.value)


def test_an_empty_idle_prompt_list_is_refused():
    """With no transcript, this list is the only way a turn can ever end."""
    write(
        '[harness.codex]\nbinary = "c"\nidle_prompts = []\n'
        "approvals = { manual = [], edits = [], yolo = [] }\n"
    )
    with pytest.raises(cfg.ConfigError, match="idle_prompts"):
        cfg.load()


def test_an_unknown_key_suggests_the_real_one():
    write(CODEX + '\nicons = "x"\n')
    with pytest.raises(cfg.ConfigError) as exc:
        cfg.load()
    assert "harness.codex.icons" in str(exc.value)
    assert "did you mean 'icon'" in str(exc.value)


def test_a_wrong_type_says_what_was_expected():
    write(CODEX.replace('binary = "codex"', "binary = 3"))
    with pytest.raises(cfg.ConfigError, match="must be a string"):
        cfg.load()


def test_approval_flags_must_be_lists_of_strings():
    write(CODEX.replace('edits = ["--full-auto"]', 'edits = "--full-auto"'))
    with pytest.raises(cfg.ConfigError, match="table of string lists"):
        cfg.load()


@pytest.mark.parametrize("name", ["Codex", "my codex", "-codex", "code/x"])
def test_a_harness_name_must_be_a_plain_token(name):
    write(f'[harness."{name}"]\nbinary = "c"\n')
    with pytest.raises(cfg.ConfigError, match="must be lowercase"):
        cfg.load()


def test_an_mcp_file_without_a_flag_is_refused():
    """It would be written, and nothing would ever read it."""
    write(CODEX + '\nmcp_file = "{}"\n')
    with pytest.raises(cfg.ConfigError, match="never passed to the harness"):
        cfg.load()


def test_an_mcp_flag_without_a_file_is_refused():
    write(CODEX + '\nmcp_file_argv = ["--config", "{config_path}"]\n')
    with pytest.raises(cfg.ConfigError, match="no file at that path"):
        cfg.load()


def test_declared_values_are_reported_with_their_source():
    loaded = declare(CODEX)
    rows = {key: (value, source) for key, value, source in cfg.describe(loaded)}
    assert rows["harness.codex.binary"] == ("codex", "config.toml")
    # Present but untouched, so the user can see what they did not set.
    assert rows["harness.codex.argv"][1] == "default"


# ---- installing ---------------------------------------------------------


def test_installing_adds_to_the_registry_and_to_describe():
    harness_registry.install(declare(CODEX))
    assert "codex" in harness_registry.HARNESSES
    names = [row["name"] for row in harness_registry.describe()]
    assert names == ["claude", "codex", "vibe"]


def test_installing_is_idempotent():
    loaded = declare(CODEX)
    harness_registry.install(loaded)
    harness_registry.install(loaded)
    assert sorted(harness_registry.HARNESSES) == ["claude", "codex", "vibe"]


def test_installing_an_empty_config_restores_the_builtins():
    harness_registry.install(declare(CODEX))
    harness_registry.install(cfg.Config())
    assert sorted(harness_registry.HARNESSES) == ["claude", "vibe"]


def test_a_declaration_cannot_replace_a_builtin():
    """The built-in reads a transcript; a declaration cannot. Silent downgrade."""
    loaded = declare(CODEX.replace("harness.codex", "harness.vibe"))
    with pytest.raises(harness_registry.ConfigError, match="built-in"):
        harness_registry.install(loaded)


def test_every_approval_mode_must_be_spelled_out():
    """An unlisted mode would launch with no flags and look deliberate."""
    loaded = declare(
        """
[harness.codex]
binary = "codex"
idle_prompts = ["›"]
approvals = { manual = [], edits = ["--full-auto"] }
"""
    )
    with pytest.raises(harness_registry.ConfigError, match="missing yolo"):
        harness_registry.install(loaded)


def test_an_unknown_approval_mode_is_refused():
    loaded = declare(CODEX.replace("manual = []", "manual = [], nuclear = []"))
    with pytest.raises(harness_registry.ConfigError, match="nuclear"):
        harness_registry.install(loaded)


def test_declared_aliases_normalize():
    loaded = declare(CODEX + '\naliases = ["codex-cli", "openai-codex"]\n')
    harness_registry.install(loaded)
    assert harness_registry.normalize("openai-codex") == "codex"
    assert harness_registry.harness_icon("codex-cli") == "@"


def test_an_alias_cannot_shadow_another_harness():
    loaded = declare(CODEX + '\naliases = ["mistral-vibe"]\n')
    with pytest.raises(harness_registry.ConfigError, match="already resolves"):
        harness_registry.install(loaded)


def test_an_alias_cannot_be_another_harness_name():
    loaded = declare(CODEX + '\naliases = ["claude"]\n')
    with pytest.raises(harness_registry.ConfigError, match="name of another"):
        harness_registry.install(loaded)


def test_a_declared_binary_joins_the_unmanaged_sweep():
    harness_registry.install(declare(CODEX))
    assert "codex" in harness_registry.known_binaries()


# ---- launching ----------------------------------------------------------


def test_render_leaves_json_braces_alone():
    """The reason this is substitution and not str.format."""
    out = render('{"mcp": {"id": "{id}"}}', {"id": "abc"})
    assert out == '{"mcp": {"id": "abc"}}'


def test_argv_order_is_approval_then_injection_then_arguments():
    h = DeclaredHarness(
        "codex",
        spec(
            mcp_argv=["-c", "cmd={theater}", "-c", "id={id}"],
            argv=["--model", "gpt", "{prompt}"],
        ),
    )
    plan = h.plan_launch(
        participant_id="abc123",
        prompt="hello",
        config_path=Path("/tmp/x.json"),
        approval="edits",
    )
    assert plan.argv[:2] == ["codex", "--full-auto"]
    assert plan.argv[2] == "-c"
    assert plan.argv[3].startswith("cmd=") and plan.argv[3].endswith("theater")
    assert plan.argv[4:6] == ["-c", "id=abc123"]
    assert plan.argv[6:] == ["--model", "gpt", "hello"]


def test_an_empty_prompt_is_dropped_rather_than_passed_as_an_argument():
    h = DeclaredHarness("codex", spec())
    plan = h.plan_launch(
        participant_id="abc123",
        prompt="",
        config_path=Path("/tmp/x.json"),
        approval="manual",
    )
    assert plan.argv == ["codex"]


def test_the_participant_id_reaches_the_environment():
    h = DeclaredHarness(
        "codex", spec(mcp_env={"CODEX_MCP": '[{"args": ["mcp", "--id", "{id}"]}]'})
    )
    plan = h.plan_launch(
        participant_id="abc123",
        prompt="hi",
        config_path=Path("/tmp/x.json"),
        approval="manual",
    )
    assert plan.env["CODEX_MCP"] == '[{"args": ["mcp", "--id", "abc123"]}]'


def test_the_mcp_file_is_written_at_the_path_it_is_pointed_at():
    h = DeclaredHarness(
        "oc",
        spec(
            mcp_file='{"cmd": ["{theater}", "mcp", "--id", "{id}"]}',
            mcp_file_argv=["--config", "{config_path}"],
        ),
    )
    plan = h.plan_launch(
        participant_id="abc123",
        prompt="hi",
        config_path=Path("/tmp/oc.json"),
        approval="manual",
    )
    assert list(plan.files) == [Path("/tmp/oc.json")]
    assert '"--id", "abc123"' in plan.files[Path("/tmp/oc.json")]
    assert "--config" in plan.argv and "/tmp/oc.json" in plan.argv


def test_an_unknown_approval_mode_is_refused_at_launch():
    h = DeclaredHarness("codex", spec())
    with pytest.raises(BadRequest, match="approval must be one of"):
        h.plan_launch(
            participant_id="a",
            prompt="",
            config_path=Path("/tmp/x"),
            approval="whatever",
        )


# ---- observing ----------------------------------------------------------


def test_a_declared_harness_admits_it_has_no_transcript():
    h = DeclaredHarness("codex", spec())
    assert h.has_transcript is False
    assert h.find_transcript(cwd="/tmp") is None
    assert h.session_id(Path("/tmp/x")) is None
    assert h.parse('{"role": "user"}', 0) == []
    assert h.native_children(Path("/tmp/x")) == []


def test_the_idle_prompt_must_match_the_whole_line():
    h = DeclaredHarness("codex", spec())
    assert h.is_idle_screen("some output\n›\n") is True
    # Text after the prompt is a human typing, which is presence, not idleness.
    assert h.is_idle_screen("some output\n› what about\n") is False
    assert h.is_idle_screen("") is False


def test_screen_result_drops_the_prompt_line():
    assert screen_result("banner\nthe answer\n›\n\n") == "banner\nthe answer"


def test_screen_result_of_a_bare_prompt_is_empty():
    assert screen_result("›\n") == ""


class Screen:
    """A pane whose contents the test sets, standing in for capture-pane."""

    def __init__(self, text: str = "working…"):
        self.text = text
        self.reads = 0

    async def capture(self, pane):
        self.reads += 1
        return self.text


async def until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return False


@pytest.fixture
def declared():
    return DeclaredHarness("codex", cfg.HarnessSpec(
        binary="codex",
        idle_prompts=["›"],
        approvals={"manual": [], "edits": [], "yolo": []},
    ))


@pytest.fixture
async def screen_observer(registry, declared, monkeypatch):
    """An observer watching a screen the test controls."""
    screen = Screen()
    observer = Observer(
        registry,
        {"codex": declared},
        sync=0.01,
        screen=0.01,
        jobs=JobManager(registry.store),
    )
    monkeypatch.setattr(observer, "_capture", screen.capture)
    observer.start()
    yield observer, screen
    await observer.aclose()


async def test_a_busy_screen_is_working(registry, screen_observer):
    observer, screen = screen_observer
    p = registry.register(harness="codex", pane="%1", cwd="/tmp")
    assert await until(lambda: registry.get(p.id).status is Status.WORKING)


async def test_a_prompt_that_holds_ends_the_turn(registry, screen_observer):
    observer, screen = screen_observer
    p = registry.register(harness="codex", pane="%1", cwd="/tmp")
    assert await until(lambda: registry.get(p.id).status is Status.WORKING)

    screen.text = "banner\nhere is the answer\n›\n"
    assert await until(lambda: registry.get(p.id).status is Status.IDLE)

    rows = [
        row
        for row in registry.store.bus_tail(limit=200)
        if row["kind"] == "agent.assistant"
    ]
    assert len(rows) == 1
    # Marked as a rendering, not a parsed record: index -1 belongs to no line.
    assert rows[0]["payload"]["source"] == "screen"
    assert rows[0]["payload"]["turn_end"] is True
    assert rows[0]["payload"]["index"] == -1
    assert rows[0]["payload"]["text"] == "banner\nhere is the answer"


async def test_one_idle_frame_is_not_a_turn_end(registry, declared, monkeypatch):
    """A pane cleared mid-work shows a bare prompt for a frame."""
    observer = Observer(registry, {"codex": declared}, sync=0.01, screen=0.01)
    captures = ["working…", "›", "still working…", "working…"]

    async def capture(pane):
        return captures.pop(0) if captures else "working…"

    monkeypatch.setattr(observer, "_capture", capture)
    observer.start()
    try:
        p = registry.register(harness="codex", pane="%1", cwd="/tmp")
        assert await until(lambda: not captures)
        await asyncio.sleep(0.05)
        assert registry.get(p.id).status == Status.WORKING
        rows = [
            row
            for row in registry.store.bus_tail(limit=200)
            if row["kind"] == "agent.assistant"
        ]
        assert rows == []
    finally:
        await observer.aclose()


async def test_the_turn_end_finishes_a_waiting_job(registry, screen_observer):
    observer, screen = screen_observer
    p = registry.register(harness="codex", pane="%1", cwd="/tmp")
    observer.jobs.create(
        handle="h1", caller_id="caller", target_id=p.id, kind="send", prompt="go"
    )
    assert await until(lambda: registry.get(p.id).status == Status.WORKING)

    screen.text = "the answer\n›\n"
    assert await until(lambda: observer.jobs.get("h1").state == JobState.DONE)
    assert observer.jobs.get("h1").result == "the answer"


async def test_a_second_turn_ends_again(registry, screen_observer):
    """The turn-end guard must reset, or only the first send ever completes."""
    observer, screen = screen_observer
    p = registry.register(harness="codex", pane="%1", cwd="/tmp")
    # A fresh participant is already IDLE, so waiting for the first IDLE would
    # be satisfied before the observer had looked at anything.
    assert await until(lambda: registry.get(p.id).status == Status.WORKING)

    screen.text = "first\n›\n"
    assert await until(lambda: registry.get(p.id).status is Status.IDLE)
    screen.text = "thinking…"
    assert await until(lambda: registry.get(p.id).status is Status.WORKING)
    screen.text = "second\n›\n"
    assert await until(lambda: registry.get(p.id).status is Status.IDLE)

    texts = [
        row["payload"]["text"]
        for row in registry.store.bus_tail(limit=200)
        if row["kind"] == "agent.assistant"
    ]
    assert texts == ["first", "second"]


async def test_an_unreadable_screen_decides_nothing(registry, declared, monkeypatch):
    observer = Observer(registry, {"codex": declared}, sync=0.01, screen=0.01)

    reads = 0

    async def capture(pane):
        nonlocal reads
        reads += 1
        return None  # capture-pane failed

    monkeypatch.setattr(observer, "_capture", capture)
    observer.start()
    try:
        p = registry.register(harness="codex", pane="%1", cwd="/tmp")
        before = registry.get(p.id).status
        await asyncio.sleep(0.1)
        assert reads > 0, "the watcher never ran, so this proves nothing"
        assert registry.get(p.id).status == before
    finally:
        await observer.aclose()


async def test_a_declared_harness_needs_no_working_directory(registry, screen_observer):
    """cwd finds a transcript. There is no transcript, so there is no cwd."""
    observer, screen = screen_observer
    p = registry.register(harness="codex", pane="%1", cwd="")
    screen.text = "done\n›\n"
    assert await until(lambda: registry.get(p.id).status is Status.IDLE)


def test_two_confirmations_is_the_documented_minimum():
    """One would make a single cleared frame finish a job."""
    assert IDLE_CONFIRMATIONS >= 2


# ---- the CLI sees the same set ------------------------------------------


def test_spawn_rejects_an_unknown_harness_and_suggests_the_flag(capsys):
    assert cli.main(["spawn", "--approval", "manual", "do the thing"]) == 1
    err = capsys.readouterr().err
    assert "unknown harness 'do the thing'" in err
    assert "--prompt" in err


def test_harnesses_lists_a_declared_one(capsys):
    write(CODEX)
    assert cli.main(["harnesses"]) == 0
    out = capsys.readouterr().out
    assert "codex" in out


def test_a_broken_declaration_stops_every_command_but_config(capsys):
    write('[harness.codex]\nbinary = "c"\napprovals = { manual = [] }\n')
    assert cli.main(["ls"]) == 1
    assert "idle_prompts" in capsys.readouterr().err
    # `config` is the one that has to keep working, since it is the command
    # that explains the file.
    assert cli.main(["config"]) == 1
    assert "idle_prompts" in capsys.readouterr().err
