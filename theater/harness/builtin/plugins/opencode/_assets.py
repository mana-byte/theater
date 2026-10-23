"""Read the checked JavaScript shipped with the OpenCode plugin."""

from importlib.resources import files


def read_asset(name: str) -> str:
    asset = files("theater.harness.builtin.plugins.opencode").joinpath("assets", name)
    return asset.read_text(encoding="utf-8")
