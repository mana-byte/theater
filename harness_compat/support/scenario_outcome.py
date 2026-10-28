"""An assertion failure and an unexpected test exception need different explanations."""

import subprocess

import pytest


def failure_outcome(error: BaseException) -> str:
    if isinstance(error, (AssertionError, pytest.fail.Exception)):
        return "failed"
    if isinstance(error, (OSError, TimeoutError, subprocess.SubprocessError)):
        return "error"
    return "tooling"
