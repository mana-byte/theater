"""Shared parser helper and scripted daemon client for the CLI command tests."""

from __future__ import annotations

from theater import cli


def parse(*argv):
    return cli._parser().parse_args(list(argv))


class StopFollow(Exception):
    """Ends a follow loop the way ctrl-c would, without the signal."""


class FollowClient:
    """A daemon that answers a fixed script, then ends the loop.

    `_watch_ls` and `_follow_bus` never return on their own, so the script
    running out is how the test gets control back.
    """

    def __init__(self, script):
        self.script = list(script)
        self.calls: list[tuple[str, dict]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def call(self, method, **params):
        self.calls.append((method, params))
        if not self.script:
            raise StopFollow
        return self.script.pop(0)
