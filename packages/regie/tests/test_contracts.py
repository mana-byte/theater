from pathlib import Path

from regie import BridgeConfig, PresentationTarget, RegieSettings


def test_wave11_shared_contract_defaults_are_stable() -> None:
    config = BridgeConfig(theater_socket=Path("/tmp/theater.sock"), state_dir=Path("/tmp/regie"))
    target = PresentationTarget(
        provider_id="provider-a",
        provider_kind="tmux",
        terminal_id="%1",
        terminal_incarnation="incarnation-a",
    )

    assert config.selector == "tmux"
    assert RegieSettings().sidebar_width == 52
    assert (target.provider_kind, target.terminal_id) == ("tmux", "%1")
