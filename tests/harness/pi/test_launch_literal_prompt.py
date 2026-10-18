"""Pi launch planning keeps handoff prompts literal for Pi's argv parser."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from theater.harness.builtin.plugins.pi.launch import plan_launch
from theater.harness.contracts.callbacks import LaunchContext

# Pi's parseArgs (packages/coding-agent/src/cli/args.ts) routes by argv prefix:
# lines ~91 (--help/-h), ~225 ("@" becomes a file arg, even after "--" per
# lines 82-88), ~227 (unknown "--" flag), ~241 ("Unknown option" for "-").
# Only the START of each element is inspected, so a leading newline lands the
# whole prompt in `messages`; buildInitialMessage then uses it verbatim.
_PI_SOURCE = Path(os.environ.get("PI_SOURCE_DIR", "/Users/manaiki.laut/Desktop/coding_clis/pi"))

HOSTILE_PROMPTS = [
    "- item: finish the migration",
    "---",
    "--help",
    "-h",
    "@teammate please review the diff",
]


def _plan_argv(prompt: str) -> list[str]:
    plan = plan_launch(
        LaunchContext(
            participant_id="pi-child",
            prompt=prompt,
            config_path=Path("mcp.json"),
            approval="yolo",
        )
    )
    return plan.argv


def _pi_argv(plan_argv: list[str]) -> list[str]:
    """The argv bootstrap hands to `pi`: it strips its own cold-session pair."""
    args = plan_argv[3:]  # drop the python -m bootstrap prefix
    assert args[:2] == ["--theater-cold-session-id", "pi-child"]
    return args[2:]


def _parse_with_pi(pi_argv: list[str]) -> dict[str, object]:
    """Run Pi's real parseArgs; skip when node or the source checkout is missing."""
    args_ts = _PI_SOURCE / "packages/coding-agent/src/cli/args.ts"
    node = shutil.which("node")
    if node is None or not args_ts.is_file():
        pytest.skip("node or the Pi source checkout is unavailable")
    # Bare imports in args.ts (chalk, cross-spawn) are uninstalled here and
    # unused by parseArgs, so unresolved bare specifiers get a stub module.
    probe = (
        "import { registerHooks } from 'node:module';\n"
        "const stub = 'data:text/javascript,"
        "export default new Proxy(function(){},{get:(t,k)=>()=>({})});';\n"
        "registerHooks({\n"
        "  resolve(specifier, context, nextResolve) {\n"
        "    try { return nextResolve(specifier, context); }\n"
        "    catch { return { shortCircuit: true, url: stub }; }\n"
        "  },\n"
        "});\n"
        f"const {{ parseArgs }} = await import({json.dumps(str(args_ts))});\n"
        "const r = parseArgs(JSON.parse(process.argv[2]));\n"
        "console.log(JSON.stringify({\n"
        "  messages: r.messages, fileArgs: r.fileArgs, help: r.help === true,\n"
        "  errors: r.diagnostics.filter((d) => d.type === 'error'),\n"
        "}));\n"
    )
    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "probe.mjs"
        script.write_text(probe)
        result = subprocess.run(
            [node, "--experimental-strip-types", str(script), json.dumps(pi_argv)],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    if result.returncode != 0:
        pytest.fail(f"pi parseArgs probe failed:\n{result.stderr}")
    return json.loads(result.stdout)


@pytest.mark.parametrize("prompt", HOSTILE_PROMPTS)
def test_pi_launch_prompt_survives_pi_parser(prompt: str) -> None:
    planned = _plan_argv(prompt)
    assert planned[-1] == f"\n{prompt}"
    parsed = _parse_with_pi(_pi_argv(planned))
    assert parsed == {
        "messages": [f"\n{prompt}"],
        "fileArgs": [],
        "help": False,
        "errors": [],
    }


def test_pi_launch_ordinary_prompt_is_unchanged() -> None:
    planned = _plan_argv("inspect this repository")
    assert planned[-1] == "inspect this repository"
    parsed = _parse_with_pi(_pi_argv(planned))
    assert parsed["messages"] == ["inspect this repository"]
