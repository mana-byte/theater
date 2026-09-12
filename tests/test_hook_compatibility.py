"""Optional hook compatibility cannot downgrade an ordinary harness launch."""

from __future__ import annotations

import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest

from theater import paths
from theater.daemon.spawning.hook_compatibility import probe_hook_channels
from theater.daemon.spawning.planning import install_hook_plan
from theater.harness.builtin.plugins.claude.compatibility import probe_claude_hooks
from theater.harness.contracts.callbacks import HookInstallOverlay
from theater.harness.contracts.channels import ChannelDeclaration, ChannelKind, HookBinding
from theater.harness.contracts.launch import LaunchPlan
from theater.harness.contracts.manifest import HookChannelManifest
from theater.harness.contracts.runtime import RuntimeCompatibility, RuntimeProbeContext
from theater.models import Participant


def _channel(name, probe, installed):
    def install(context):
        installed.append(context.channel_id)
        return HookInstallOverlay()

    return HookChannelManifest(
        declaration=ChannelDeclaration(id=name, kind=ChannelKind.HOOK),
        bindings=(
            HookBinding(
                event="tool",
                signals=(),
                decoder=lambda _context: (),
                correlation=lambda _context: "tool-id",
            ),
        ),
        installer=install,
        probe=probe,
    )


def _harness(*channels):
    return SimpleNamespace(
        binary="example-cli",
        observer=SimpleNamespace(enrichment_manifests=lambda: channels),
    )


@pytest.mark.asyncio
async def test_probe_is_off_loop_and_failed_optional_hooks_never_install(theater_home):
    installed = []
    main_thread = threading.get_ident()

    def supported(context):
        assert threading.get_ident() != main_thread
        assert context.binary == "example-cli"
        assert context.participant_id == "hook-probe"
        return RuntimeCompatibility(supported=True)

    def broken(_context):
        raise OSError("unavailable executable")

    harness = _harness(
        _channel("existing", None, installed),
        _channel("supported", supported, installed),
        _channel("unsupported", lambda _: RuntimeCompatibility(supported=False), installed),
        _channel("broken", broken, installed),
        _channel("malformed", lambda _: True, installed),
    )
    participant = Participant(id="hook-probe", harness="example")
    selected = await probe_hook_channels(participant, harness)
    assert selected == {"existing", "supported"}
    baseline = LaunchPlan(argv=["example-cli", "prompt"])
    plan = install_hook_plan(baseline, participant, harness.observer, enabled_channels=selected)
    assert installed == ["existing", "supported"]
    assert plan.argv == baseline.argv
    assert {item.channel_id for item in plan.channel_credentials} == selected


@pytest.mark.asyncio
async def test_legacy_optout_does_not_run_optional_probe_or_remove_existing_hooks(theater_home):
    installed = []

    def should_not_run(_context):
        pytest.fail("legacy opt-out must not probe optional native hooks")

    harness = _harness(
        _channel("existing", None, installed),
        _channel("optional", should_not_run, installed),
    )
    participant = Participant(id="legacy-hook-probe", harness="example")
    selected = await probe_hook_channels(participant, harness, native_enabled=False)
    plan = install_hook_plan(
        LaunchPlan(argv=["example-cli"]),
        participant,
        harness.observer,
        enabled_channels=selected,
    )
    assert installed == ["existing"]
    assert [item.channel_id for item in plan.channel_credentials] == ["existing"]


def test_direct_install_cannot_bypass_probe(theater_home):
    installed = []
    harness = _harness(
        _channel("optional", lambda _: RuntimeCompatibility(supported=True), installed)
    )
    baseline = LaunchPlan(argv=["example-cli"])
    plan = install_hook_plan(
        baseline, Participant(id="unprobed", harness="example"), harness.observer
    )
    assert plan is baseline
    assert installed == []


def test_optional_installer_failure_rolls_back_replacements_and_credentials(theater_home):
    participant = Participant(id="failed-install", harness="example")
    settings = paths.participant_launch_dir(participant.id) / "settings.json"
    baseline = LaunchPlan(
        argv=["example-cli"], files={settings: "original"}, env={"EXISTING": "original"}
    )
    channel = replace(
        _channel("optional", lambda _: RuntimeCompatibility(supported=True), []),
        installer=lambda _: HookInstallOverlay(
            replacements={settings: "modified"}, env={"EXISTING": "collision"}
        ),
    )
    plan = install_hook_plan(
        baseline,
        participant,
        _harness(channel).observer,
        enabled_channels=frozenset({"optional"}),
    )
    assert plan is baseline
    assert plan.files[settings] == "original"
    assert not plan.channel_credentials


@pytest.mark.parametrize(
    ("version", "supported"),
    [
        ("2.1.201 (Claude Code)", False),
        ("2.1.202 (Claude Code)", True),
        ("2.1.220 (Claude Code)", True),
        ("2.2.0 (Claude Code)", True),
        ("3.0.0 (Claude Code)", False),
        ("2.1.220-beta (Claude Code)", False),
        ("unknown", False),
    ],
)
def test_claude_hook_version_range(version, supported, monkeypatch):
    monkeypatch.setattr(
        "theater.harness.builtin.plugins.claude.compatibility.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=version),
    )
    assert probe_claude_hooks(RuntimeProbeContext()).supported is supported
