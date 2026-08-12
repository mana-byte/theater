"""The harnesses Theater ships with.

`plugins/` holds them, and it is a directory of plugin *files* rather than a
package of imported modules on purpose: they are loaded by the same loader,
through the same `HARNESS = ...()` contract and the same validation, as
anything a user drops in `$THEATER_HOME/harnesses`. Shipping them any other way
would leave the extension point untested by everything except the extensions.

So there is no "built-in tier". The only distinction the system still draws
between adapters is `HarnessObserver.has_transcript`.
"""

from __future__ import annotations

from pathlib import Path


def plugin_dir() -> Path:
    """Where the shipped plugins live, as a real directory on disk.

    A plain filesystem path, not `importlib.resources`: the loader reads files
    by path, and Theater is installed from source or as a wheel that unpacks to
    a directory, never from a zipimport. If that ever changes this is the one
    function that has to learn about it.
    """
    return Path(__file__).resolve().parent / "plugins"
