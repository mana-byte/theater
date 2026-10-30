"""An adapter's provisioning: its values are frozen, its declarations bounded, payloads render."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from harness_compat.contract import MockToolCall
from harness_compat.mock.provisioning import (
    TOOL_NAME_MAX_CHARS,
    TOOL_PAYLOAD_MAX_CHARS,
    CompatManifest,
    McpCall,
    McpDiscovery,
    NativeEdit,
    ProvisionContext,
    Provisioned,
)


def provision(context: ProvisionContext) -> Provisioned:
    return Provisioned(
        protocol="openai-chat",
        env={"ACME_BASE_URL": context.mock_urls["openai-chat"]},
        files={context.config_dir / "acme.toml": "x = 1\n"},
        models=context.models,
        efforts=("low", "high"),
    )


def context(tmp_path: Path) -> ProvisionContext:
    return ProvisionContext(
        sandbox_root=tmp_path,
        home=tmp_path / "home",
        config_dir=tmp_path / "config",
        workdir=tmp_path / "work",
        mock_urls={"openai-chat": "http://127.0.0.1:1/v1"},
        binary=tmp_path / "acme",
        version="1.2.3",
        models=["mock-a", "mock-b"],
    )


def test_context_and_provisioned_values_are_frozen(tmp_path: Path) -> None:
    ctx = context(tmp_path)
    provisioned = provision(ctx)

    assert ctx.models == ("mock-a", "mock-b")
    for frozen in (ctx.mock_urls, provisioned.env, provisioned.files):
        with pytest.raises(TypeError):
            frozen["x"] = "y"


def test_provisioned_rejects_an_unknown_protocol() -> None:
    with pytest.raises(ValueError, match="protocol must be one of"):
        Provisioned(protocol="gemini")


def test_hook_stimuli_freeze_calls_and_preserve_plain_prompt_selection() -> None:
    payload = {"command": "true"}
    events = {"native/start": MockToolCall("Bash", payload), "native/stop": None}
    compat = CompatManifest(provision=provision, hook_events=events)
    payload["command"] = "different"
    events.clear()
    assert compat.hook_events["native/start"].arguments == {"command": "true"}
    assert compat.hook_events["native/stop"] is None
    with pytest.raises(TypeError):
        compat.hook_events["native/stop"] = MockToolCall("Bash", {})
    with pytest.raises(TypeError):
        compat.hook_events["native/start"].arguments["command"] = "different"


@pytest.mark.parametrize(
    ("declare", "message"),
    [
        pytest.param(
            lambda: CompatManifest(provision=provision, canaries=("a", "a")),
            "each mutation once",
            id="canary-duplicate-name",
        ),
        pytest.param(
            lambda: CompatManifest(provision=provision, acknowledged_routes=(("PUT", "/x"),)),
            "GET or POST",
            id="route-method",
        ),
        pytest.param(
            lambda: CompatManifest(provision=provision, acknowledged_routes=(("POST", "x"),)),
            "a /path",
            id="route-relative-path",
        ),
        pytest.param(lambda: NativeEdit(" ", "{path}"), "non-blank", id="blank-name"),
        pytest.param(
            lambda: CompatManifest(
                provision=provision, hook_events={"native/start": MockToolCall(" ", {})}
            ),
            "non-blank",
            id="hook-stimulus-blank-name",
        ),
        pytest.param(
            lambda: NativeEdit("w" * (TOOL_NAME_MAX_CHARS + 1), "{path}"),
            f"at most {TOOL_NAME_MAX_CHARS}",
            id="long-name",
        ),
        pytest.param(lambda: NativeEdit("write", "  "), "must not be blank", id="blank-payload"),
        pytest.param(
            lambda: NativeEdit("write", "x" * TOOL_PAYLOAD_MAX_CHARS + "{path}"),
            f"at most {TOOL_PAYLOAD_MAX_CHARS}",
            id="long-payload",
        ),
        pytest.param(
            lambda: NativeEdit("write", {"path": "x"}),
            "{path} placeholder",
            id="edit-without-path-placeholder",
        ),
        pytest.param(
            lambda: McpCall("run", {"code": "{arguments}"}),
            "{tool} placeholder",
            id="call-without-tool-placeholder",
        ),
        pytest.param(
            lambda: McpCall("run", {"code": "{tool}()"}),
            "{arguments} placeholder",
            id="call-without-arguments-placeholder",
        ),
        pytest.param(
            lambda: McpCall("run", {"code": "{path}{tool}"}),
            "{arguments} placeholder",
            id="call-foreign-token-is-literal",
        ),
        pytest.param(
            lambda: McpDiscovery("search", {"query": "{server}"}),
            "{tool} placeholder",
            id="discovery-without-tool-placeholder",
        ),
    ],
)
def test_a_malformed_declaration_is_refused_when_declared(declare, message: str) -> None:
    with pytest.raises(ValueError, match=re.escape(message)):
        declare()


@pytest.mark.parametrize(
    "edit",
    [
        pytest.param(NativeEdit("write", "{path}"), id="freeform"),
        pytest.param(NativeEdit("write_file", {"path": "{path}", "content": "{nonce}"}), id="json"),
        pytest.param(NativeEdit("patch", "*** Add File: {path}\n+{nonce}\n"), id="patch"),
    ],
)
def test_a_well_formed_native_edit_is_declared(edit: NativeEdit) -> None:
    CompatManifest(provision=provision, native_edit=edit)


def test_native_edit_renders_placeholders_in_one_pass_and_leaves_other_braces() -> None:
    edit = NativeEdit(
        "write_file",
        {"file": "{path}", "lines": ["{nonce}", "{keep}", {"deep": "x{path}y"}], "n": 3},
    )

    rendered = edit.render(path="/tmp/a{nonce}", nonce="N1")

    assert rendered == {
        "file": "/tmp/a{nonce}",
        "lines": ("N1", "{keep}", {"deep": "x/tmp/a{nonce}y"}),
        "n": 3,
    }
    assert edit.placeholders == {"path", "nonce"}


def test_mcp_call_substitutes_its_tokens_once_and_encodes_arguments_as_json() -> None:
    call = McpCall("run_script", {"code": "await tools.mcp_{server}.{tool}({arguments});"})

    rendered = call.render(server="probe", tool="mark", arguments={"label": "{tool}", "n": 2})

    assert rendered == {
        "code": 'await tools.mcp_probe.mark({"label": "{tool}", "n": 2});',
    }
    assert call.placeholders == {"server", "tool", "arguments"}


def test_each_template_recognises_only_its_own_tokens() -> None:
    assert NativeEdit("w", "{path}{tool}").placeholders == {"path"}
    assert McpCall("c", "{tool}{path}{arguments}").placeholders == {"tool", "arguments"}
    assert McpDiscovery("s", "{server}{tool}{arguments}").placeholders == {"server", "tool"}


def test_mcp_discovery_renders_its_two_tokens() -> None:
    search = McpDiscovery("search", {"query": "select:mcp__{server}__{tool}"})

    assert search.render(server="probe", tool="mark") == {"query": "select:mcp__probe__mark"}


def test_native_edit_freeform_payload_renders_as_text() -> None:
    edit = NativeEdit("apply_patch", "*** Add File: {path}\n+{nonce}\n")

    assert edit.render(path="p.txt", nonce="N") == "*** Add File: p.txt\n+N\n"
    assert edit.placeholders == {"path", "nonce"}
