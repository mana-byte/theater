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


def daemon_stub(
    *,
    reply=None,
    raises=None,
    enter_raises=None,
    expect_kwargs=None,
    expect_call=None,
    expect_method=None,
):
    """A DaemonClient stand-in: answers `reply`, or raises `raises(method)` / `enter_raises`."""

    class Stub:
        def __init__(self, *args, **kwargs):
            if expect_kwargs is not None:
                assert not args and kwargs == expect_kwargs

        async def __aenter__(self):
            if enter_raises is not None:
                raise enter_raises
            return self

        async def __aexit__(self, *exc):
            return False

        async def call(self, method, **params):
            if expect_method is not None:
                assert method == expect_method
            if expect_call is not None:
                assert (method, params) == expect_call
            if raises is not None:
                raise raises(method)
            return reply

    return Stub
