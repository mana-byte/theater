"""Optional hook compatibility cannot downgrade an ordinary harness launch."""

from __future__ import annotations

import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest

from theater import paths
from theater.daemon.spawning.hook_compatibility import probe_hook_channels
from theater.daemon.spawning.planning import install_hook_plan
from theater.harness.builtin.plugins.claude.compatibility import (
    VERSION_POLICY,
    claude_messaging_range_text,
    probe_claude_hooks,
    probe_claude_native_compatibility,
)
from theater.harness.contracts.callbacks import HookInstallOverlay
from theater.harness.contracts.channels import ChannelDeclaration, ChannelKind, HookBinding
from theater.harness.contracts.launch import LaunchPlan
from theater.harness.contracts.manifest import HookChannelManifest
from theater.harness.contracts.runtime import RuntimeCompatibility, RuntimeProbeContext
from theater.harness.contracts.version_policy import version_key
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


def _cli_version(parts: tuple[int, int, int]) -> str:
    return f"{'.'.join(map(str, parts))} (Claude Code)"


_MINIMUM = version_key(VERSION_POLICY.minimum)
_MAXIMUM = version_key(VERSION_POLICY.maximum)


@pytest.mark.parametrize(
    ("sample", "supported"),
    [
        pytest.param((*_MINIMUM[:2], _MINIMUM[2] - 1), False, id="below-minimum"),
        pytest.param(_MINIMUM, True, id="minimum"),
        pytest.param((*_MINIMUM[:2], _MINIMUM[2] + 1), True, id="next-patch"),
        pytest.param(_MAXIMUM, True, id="maximum"),
        pytest.param(
            (*_MAXIMUM[:2], _MAXIMUM[2] + 1),
            False,
            id="hook-above-max",
        ),
    ],
)
def test_claude_hook_version_range(sample, supported, monkeypatch):
    version = _cli_version(sample)
    monkeypatch.setattr(
        "theater.harness.builtin.plugins.claude.compatibility.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=version),
    )
    result = probe_claude_hooks(RuntimeProbeContext())
    assert result.supported is supported
    assert result.policy == f"claude-hooks-{VERSION_POLICY.minimum}-compatible"
    assert result.reason == (
        None if supported else f"Claude support requires {VERSION_POLICY.range_text}"
    )


@pytest.mark.parametrize("version", ["unknown", f"{_cli_version(_MINIMUM)}-beta", "02.1.296"])
def test_claude_hook_rejects_malformed_version(version, monkeypatch):
    monkeypatch.setattr(
        "theater.harness.builtin.plugins.claude.compatibility.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=version),
    )
    assert probe_claude_hooks(RuntimeProbeContext()).reason == (
        "Claude did not report a stable CLI version"
    )


@pytest.mark.parametrize(
    ("sample", "supported"),
    [
        ((*_MINIMUM[:2], _MINIMUM[2] - 1), False),
        (_MINIMUM, True),
        (_MAXIMUM, True),
        ((*_MAXIMUM[:2], _MAXIMUM[2] + 1), False),
        ((_MAXIMUM[0] + 1, 0, 0), False),
    ],
)
def test_claude_messaging_uses_the_same_bounded_policy(sample, supported, monkeypatch):
    version = _cli_version(sample)
    monkeypatch.setattr(
        "theater.harness.builtin.plugins.claude.compatibility.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=version),
    )
    result = probe_claude_native_compatibility(RuntimeProbeContext())
    assert result.supported is supported
    assert result.native_version == ".".join(map(str, sample))
    assert result.policy == (f"claude-messaging-native-controls-{VERSION_POLICY.minimum}")
    assert result.reason == (
        None if supported else f"Claude support requires {claude_messaging_range_text()}"
    )
