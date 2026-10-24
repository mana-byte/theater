"""The optional compat manifest: compilation forwards it, validation bounds it, payloads render."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from tests.support.manifest_rig import manifest, plan
from theater.constants.harness import (
    HARNESS_COMPAT_TOOL_NAME_MAX_CHARS,
    HARNESS_COMPAT_TOOL_PAYLOAD_MAX_CHARS,
)
from theater.harness.contracts.compat import (
    COMPAT_PROTOCOLS,
    CompatManifest,
    McpCall,
    NativeEdit,
    ProvisionContext,
    Provisioned,
)
from theater.harness.contracts.manifest import LaunchManifest
from theater.harness.manifests.compiler import compile_manifest
from theater.harness.manifests.validation import ManifestValidationError


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


def test_a_harness_without_compat_compiles_and_declares_none() -> None:
    assert compile_manifest("acme", manifest()).compat is None


def test_compilation_forwards_the_declared_compat_manifest(tmp_path: Path) -> None:
    declared = CompatManifest(provision=provision, native_edit=NativeEdit("write", "{path}"))

    harness = compile_manifest("acme", manifest(compat=declared))

    assert harness.compat is declared
    provisioned = harness.compat.provision(context(tmp_path))
    assert provisioned.protocol == "openai-chat"
    assert provisioned.models == ("mock-a", "mock-b")
    assert provisioned.env == {"ACME_BASE_URL": "http://127.0.0.1:1/v1"}


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
    assert COMPAT_PROTOCOLS == ("anthropic", "openai-chat", "openai-responses")


_APPROVALS = ("manual", "edits", "yolo")
_EDIT = NativeEdit("write_file", {"path": "{path}", "content": "{nonce}"})


@pytest.mark.parametrize(
    ("compat", "approvals", "path", "message"),
    [
        pytest.param("compat", _APPROVALS, "compat", "expected CompatManifest", id="wrong-type"),
        pytest.param(
            CompatManifest(provision=None),
            _APPROVALS,
            "compat.provision",
            "must be callable",
            id="provision-not-callable",
        ),
        pytest.param(
            CompatManifest(provision=provision, native_edit="write"),
            _APPROVALS,
            "compat.native_edit",
            "expected NativeEdit",
            id="edit-wrong-type",
        ),
        pytest.param(
            CompatManifest(provision=provision, native_edit=_EDIT),
            ("manual", "yolo"),
            "compat.native_edit",
            "requires launch.approvals to include 'edits'",
            id="edit-without-edits-approval",
        ),
        pytest.param(
            CompatManifest(provision=provision, native_edit=NativeEdit(" ", "{path}")),
            _APPROVALS,
            "compat.native_edit.name",
            "non-blank",
            id="edit-blank-name",
        ),
        pytest.param(
            CompatManifest(
                provision=provision,
                native_edit=NativeEdit("w" * (HARNESS_COMPAT_TOOL_NAME_MAX_CHARS + 1), "{path}"),
            ),
            _APPROVALS,
            "compat.native_edit.name",
            "at most",
            id="edit-long-name",
        ),
        pytest.param(
            CompatManifest(provision=provision, native_edit=NativeEdit("write", "  ")),
            _APPROVALS,
            "compat.native_edit.payload",
            "must not be blank",
            id="edit-blank-payload",
        ),
        pytest.param(
            CompatManifest(provision=provision, native_edit=NativeEdit("write", 5)),
            _APPROVALS,
            "compat.native_edit.payload",
            "string or a JSON object",
            id="edit-payload-type",
        ),
        pytest.param(
            CompatManifest(
                provision=provision,
                native_edit=NativeEdit(
                    "write", "x" * HARNESS_COMPAT_TOOL_PAYLOAD_MAX_CHARS + "{path}"
                ),
            ),
            _APPROVALS,
            "compat.native_edit.payload",
            "at most",
            id="edit-long-payload",
        ),
        pytest.param(
            CompatManifest(provision=provision, native_edit=NativeEdit("write", {"path": "x"})),
            _APPROVALS,
            "compat.native_edit.payload",
            "{path} placeholder",
            id="edit-without-path-placeholder",
        ),
        pytest.param(
            CompatManifest(provision=provision, mcp_call="run"),
            _APPROVALS,
            "compat.mcp_call",
            "expected McpCall",
            id="call-wrong-type",
        ),
        pytest.param(
            CompatManifest(provision=provision, mcp_call=McpCall(" ", "{tool}{arguments}")),
            _APPROVALS,
            "compat.mcp_call.name",
            "non-blank",
            id="call-blank-name",
        ),
        pytest.param(
            CompatManifest(provision=provision, mcp_call=McpCall("run", {"code": "{arguments}"})),
            _APPROVALS,
            "compat.mcp_call.payload",
            "{tool} placeholder",
            id="call-without-tool-placeholder",
        ),
        pytest.param(
            CompatManifest(provision=provision, mcp_call=McpCall("run", {"code": "{tool}()"})),
            _APPROVALS,
            "compat.mcp_call.payload",
            "{arguments} placeholder",
            id="call-without-arguments-placeholder",
        ),
        pytest.param(
            CompatManifest(provision=provision, mcp_call=McpCall("run", {"code": "{path}{tool}"})),
            _APPROVALS,
            "compat.mcp_call.payload",
            "{arguments} placeholder",
            id="call-foreign-token-is-literal",
        ),
    ],
)
def test_validation_rejects_a_malformed_compat_declaration(
    compat: object, approvals: tuple[str, ...], path: str, message: str
) -> None:
    launch = LaunchManifest(planner=plan, approvals=approvals)

    with pytest.raises(ManifestValidationError) as raised:
        compile_manifest("acme", replace(manifest(launch=launch), compat=compat))

    assert raised.value.path == path
    assert message in raised.value.message


@pytest.mark.parametrize(
    "edit",
    [
        pytest.param(NativeEdit("write", "{path}"), id="freeform"),
        pytest.param(NativeEdit("write_file", {"path": "{path}", "content": "{nonce}"}), id="json"),
        pytest.param(NativeEdit("patch", "*** Add File: {path}\n+{nonce}\n"), id="patch"),
    ],
)
def test_a_well_formed_native_edit_validates(edit: NativeEdit) -> None:
    compile_manifest("acme", manifest(compat=CompatManifest(provision=provision, native_edit=edit)))


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


def test_a_well_formed_mcp_call_validates_without_an_edits_approval() -> None:
    declared = CompatManifest(
        provision=provision, mcp_call=McpCall("run", {"code": "tools.{server}.{tool}({arguments})"})
    )

    harness = compile_manifest(
        "acme",
        manifest(launch=LaunchManifest(planner=plan, approvals=("manual",)), compat=declared),
    )

    assert harness.compat is declared


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


def test_native_edit_freeform_payload_renders_as_text() -> None:
    edit = NativeEdit("apply_patch", "*** Add File: {path}\n+{nonce}\n")

    assert edit.render(path="p.txt", nonce="N") == "*** Add File: p.txt\n+N\n"
    assert edit.placeholders == {"path", "nonce"}
