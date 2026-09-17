"""Run an explicitly selected candidate executable or module in a clean child environment.

Use --candidate-executable PATH -- [args], or
--candidate-python PATH --candidate-module NAME -- [args].
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rc10_support.isolation import CandidateIsolationError, CleanupBlocked, create_candidate_sandbox


def _absolute_file(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
        raise argparse.ArgumentTypeError(f"must be an executable absolute file: {value}")
    return path


def _absolute_directory(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute() or not path.is_dir():
        raise argparse.ArgumentTypeError(f"must be an existing absolute directory: {value}")
    return path


def parser() -> argparse.ArgumentParser:
    """Build the documented argv interface without selecting a default Theater CLI."""
    result = argparse.ArgumentParser(
        description="Run one explicit candidate target with private Theater and tmux roots.",
        epilog=(
            "The runner never changes its own environment. It removes inherited Theater, tmux, "
            "and runtime-routing values only from the child, then reports a preserved root if "
            "cleanup finds any remaining Unix socket."
        ),
    )
    targets = result.add_mutually_exclusive_group(required=True)
    targets.add_argument("--candidate-executable", type=_absolute_file, metavar="PATH")
    targets.add_argument("--candidate-module", metavar="NAME")
    result.add_argument("--candidate-python", type=_absolute_file, metavar="PATH")
    result.add_argument("--candidate-cwd", type=_absolute_directory, required=True, metavar="PATH")
    result.add_argument("arguments", nargs=argparse.REMAINDER, metavar="ARG")
    return result


def _command(arguments: argparse.Namespace, argument_parser: argparse.ArgumentParser) -> list[str]:
    trailing = arguments.arguments
    if trailing[:1] == ["--"]:
        trailing = trailing[1:]
    if arguments.candidate_executable is not None:
        if arguments.candidate_python is not None:
            argument_parser.error("--candidate-python only applies with --candidate-module")
        return [str(arguments.candidate_executable), *trailing]
    if arguments.candidate_python is None:
        argument_parser.error("--candidate-module requires --candidate-python PATH")
    return [str(arguments.candidate_python), "-m", arguments.candidate_module, *trailing]


def main(argv: list[str] | None = None) -> int:
    """Run one child and preserve its roots when cleanup cannot prove safety."""
    argument_parser = parser()
    arguments = argument_parser.parse_args(argv)
    command = _command(arguments, argument_parser)
    sandbox = create_candidate_sandbox()
    exit_code: int | None = None
    launch_error: BaseException | None = None
    try:
        process = sandbox.start(command, base_environment=os.environ, cwd=arguments.candidate_cwd)
        exit_code = process.wait()
    except BaseException as exc:
        launch_error = exc

    try:
        sandbox.cleanup()
    except CleanupBlocked as exc:
        print(f"run_candidate: {exc}", file=sys.stderr)
        return 1 if exit_code in {None, 0} else exit_code

    if launch_error is not None:
        raise launch_error
    assert exit_code is not None
    return exit_code


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except CandidateIsolationError as exc:
        print(f"run_candidate: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
