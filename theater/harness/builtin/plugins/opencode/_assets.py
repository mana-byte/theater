"""Read the checked JavaScript shipped with the OpenCode plugin."""

from importlib.resources import files


def read_asset(name: str) -> str:
    return files("theater.harness.builtin.plugins.opencode").joinpath("assets", name).read_text()
