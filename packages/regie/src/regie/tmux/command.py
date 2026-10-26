"""Small asynchronous tmux command boundary owned by Régie."""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import os
import shutil
import stat
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from theater.frontend import SANDBOX_ENV

RUN_TIMEOUT_SECONDS = 10.0


class TmuxError(RuntimeError):
    """A tmux command or identity check failed."""


class TmuxMissing(TmuxError):
    """The tmux executable is unavailable."""


class TmuxOutcomeUnknown(TmuxError):
    """A terminal side effect may have begun and must not be replayed."""


@dataclass(frozen=True, slots=True)
class TmuxSocket:
    """One server address, independent of later environment or directory changes."""

    path: Path
    temporary_root: Path | None = None
    private_roots: tuple[Path, ...] = ()
    default_directory: bool = False

    def validate(self, *, preparing: bool = False) -> None:
        try:
            if self.temporary_root is not None:
                _usable_root(self.temporary_root)
            if self.path.resolve() != self.path:
                raise TmuxError(f"the pinned tmux socket path changed: {self.path}")
            if self.private_roots and not any(
                self.path.is_relative_to(root) for root in self.private_roots
            ):
                raise TmuxError(f"tmux socket is outside the sandbox home/state: {self.path}")
            parent = self.path.parent
            if preparing and self.default_directory and not parent.exists():
                return
            mode = parent.lstat()
            if not stat.S_ISDIR(mode.st_mode):
                raise TmuxError(f"tmux socket parent is not a directory: {parent}")
            if self.default_directory and (mode.st_uid != os.getuid() or mode.st_mode & 0o007):
                raise TmuxError(f"tmux socket directory has unsafe ownership/permissions: {parent}")
        except OSError as exc:
            raise TmuxError(f"the pinned tmux socket is unusable: {self.path}: {exc}") from exc

    def prepare(self) -> None:
        """Explicit -S bypasses tmux's creation and validation of its default directory."""
        self.validate(preparing=True)
        if self.default_directory:
            try:
                self.path.parent.mkdir(mode=0o700, exist_ok=True)
            except OSError as exc:
                raise TmuxError(
                    f"cannot prepare tmux socket directory: {self.path.parent}"
                ) from exc
        self.validate()

    def require_address(self, path: str | Path) -> None:
        if Path(path).resolve() != self.path:
            raise TmuxError("tmux command tried to leave its pinned server socket")


_SOCKET: contextvars.ContextVar[TmuxSocket | None] = contextvars.ContextVar(
    "regie_tmux_socket", default=None
)


def _usable_root(path: Path) -> Path:
    try:
        resolved = path.resolve(strict=True)
        if not resolved.is_dir() or not os.access(resolved, os.W_OK | os.X_OK):
            raise TmuxError(f"TMUX_TMPDIR is not a usable directory: {path}")
    except OSError as exc:
        raise TmuxError(f"TMUX_TMPDIR is not a usable directory: {path}") from exc
    else:
        return resolved


def resolve_socket(
    *,
    state_dir: Path | None = None,
    socket_path: str | Path | None = None,
    environment: Mapping[str, str] | None = None,
) -> TmuxSocket:
    """Select tmux's ordinary address once, refusing its unsafe temporary-root fallback."""
    if socket_path is not None and not str(socket_path):
        raise TmuxError("the explicit tmux socket path is empty")
    env = os.environ if environment is None else environment
    temporary_root = None
    if "TMUX_TMPDIR" in env:
        if not env["TMUX_TMPDIR"]:
            raise TmuxError("TMUX_TMPDIR is empty; refusing to use the default tmux server")
        temporary_root = _usable_root(Path(env["TMUX_TMPDIR"]))
    roots: tuple[Path, ...] = ()
    if env.get(SANDBOX_ENV):
        if temporary_root is None or not env.get("HOME"):
            raise TmuxError("a sandbox tmux provider requires private HOME and TMUX_TMPDIR")
        private_paths = [Path(env["HOME"])]
        if env.get("THEATER_HOME"):
            private_paths.append(Path(env["THEATER_HOME"]))
        if state_dir is not None:
            private_paths.append(state_dir)
        roots = tuple(path.resolve() for path in private_paths)
    selected = socket_path if socket_path is not None else env.get("TMUX", "").split(",", 1)[0]
    default_directory = not selected
    if selected:
        path = Path(selected).resolve()
    else:
        root = temporary_root if temporary_root is not None else Path("/tmp").resolve(strict=True)
        path = root / f"tmux-{os.getuid()}" / "default"
    socket = TmuxSocket(path, temporary_root, roots, default_directory)
    socket.validate(preparing=True)
    return socket


@contextlib.contextmanager
def bind_socket(socket: TmuxSocket) -> Iterator[None]:
    """Child tasks inherit the binding; teardown must finish before it is reset."""
    token = _SOCKET.set(socket)
    try:
        yield
    finally:
        _SOCKET.reset(token)


def command_argv(*args: str) -> tuple[str, ...]:
    """Every subprocess names one validated socket, including already-explicit callers."""
    socket = _SOCKET.get()
    if args[:1] == ("-S",):
        if len(args) < 3:
            raise TmuxError("tmux -S requires a socket path and command")
        if socket is None:
            socket = resolve_socket(socket_path=args[1])
        else:
            socket.require_address(args[1])
        args = args[2:]
    if socket is None:
        raise TmuxError("tmux commands require a pinned server socket")
    if not args or (args[0].startswith("-") and args != ("-V",)):
        raise TmuxError("tmux commands cannot override the pinned server selector")
    socket.validate()
    return ("tmux", "-S", str(socket.path), *args)


def available() -> bool:
    return shutil.which("tmux") is not None


def sequence_argv(commands: Sequence[Sequence[str]]) -> tuple[str, ...]:
    """Encode an ordered tmux sequence that stops at its first command error."""
    if not commands:
        raise ValueError("a tmux command sequence cannot be empty")
    argv: list[str] = []
    for command in commands:
        if not command:
            raise ValueError("a tmux command sequence cannot contain an empty command")
        if argv:
            argv.append(";")
        # tmux treats trailing semicolons as separators even without a shell.
        argv.extend(value[:-1] + "\\;" if value.endswith(";") else value for value in command)
    return tuple(argv)


async def run(
    *args: str,
    check: bool = True,
    input_bytes: bytes | None = None,
    environment: Mapping[str, str] | None = None,
) -> str:
    """Run tmux without a shell and return decoded standard output."""
    if not available():
        raise TmuxMissing("tmux is not on PATH")
    process = await asyncio.create_subprocess_exec(
        *command_argv(*args),
        stdin=asyncio.subprocess.PIPE if input_bytes is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=None if environment is None else dict(environment),
    )
    try:
        output, error = await asyncio.wait_for(
            process.communicate(input_bytes), timeout=RUN_TIMEOUT_SECONDS
        )
    except (TimeoutError, asyncio.CancelledError) as exc:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        await process.wait()
        if isinstance(exc, asyncio.CancelledError):
            raise
        raise TmuxError(f"tmux {args[0] if args else ''} timed out") from None
    if check and process.returncode != 0:
        detail = (error or output).decode("utf-8", "backslashreplace").strip()
        raise TmuxError(f"tmux {args[0] if args else ''} failed: {detail}")
    return output.decode("utf-8", "backslashreplace").rstrip("\n")


async def run_command(
    args: Sequence[str],
    *,
    check: bool = True,
    input_bytes: bytes | None = None,
) -> str:
    return await run(*args, check=check, input_bytes=input_bytes)


__all__ = [
    "TmuxError",
    "TmuxMissing",
    "TmuxOutcomeUnknown",
    "TmuxSocket",
    "available",
    "bind_socket",
    "command_argv",
    "resolve_socket",
    "run",
    "run_command",
    "sequence_argv",
]
