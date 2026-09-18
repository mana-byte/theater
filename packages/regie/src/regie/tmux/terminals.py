"""Tmux terminal creation, inspection, delivery, and exact termination."""

from __future__ import annotations

import re
import secrets
from collections.abc import Callable, Mapping, Sequence

from regie.tmux.command import TmuxError, TmuxOutcomeUnknown, run
from regie.tmux.identity import (
    PaneSnapshot,
    current_server_identity,
    exact_match,
    mark_pane,
    occupant_digest,
    pane_inventory,
    pane_snapshot,
)
from regie.tmux.presence import PresenceEvidence, observe_presence

_SAFE_SESSION = "regie-provider"
_BUFFER_PREFIX = "regie-provider-"
_MAX_TERMINALS = 500
_SAFE_KEY = re.compile(r"^[A-Za-z0-9_+@./:-]{1,128}$")
_PROVISIONAL_WINDOW = re.compile(r"^regie-launch-[A-Za-z0-9_-]{12,64}$")
_VERSION_PATTERN = re.compile(r"\d+(?:\.\d+)*")
_UNPROBED = object()
_VERSION_CACHE: list[tuple[int, ...] | object | None] = [_UNPROBED]


async def ensure_server(*, cwd: str) -> str:
    sessions = await run("list-sessions", "-F", "#{session_name}", check=False)
    if not sessions.splitlines():
        await run("new-session", "-d", "-s", _SAFE_SESSION, "-c", cwd)
    return await current_server_identity()


def terminal_identity(
    snapshot: PaneSnapshot, *, provider_id: str, generation: int
) -> dict[str, object]:
    if not snapshot.managed or snapshot.occupant_id is None:
        raise TmuxError("tmux pane has incomplete provider identity")
    assert snapshot.terminal_incarnation is not None
    assert snapshot.launch_id is not None
    assert snapshot.occupant_pane_pid is not None
    occupant: dict[str, object] = {
        "occupant_id": snapshot.occupant_id,
        "provider_kind": "tmux",
        "tmux_server_identity": snapshot.server_identity,
        "terminal_incarnation": snapshot.terminal_incarnation,
        "pane_pid": snapshot.occupant_pane_pid,
    }
    process: dict[str, object] = {"pid": snapshot.occupant_pane_pid}
    return {
        "provider_id": provider_id,
        "provider_generation": generation,
        "terminal_id": snapshot.pane_id,
        "terminal_incarnation": snapshot.terminal_incarnation,
        "occupant": occupant,
        "process": process,
        "launch_id": snapshot.launch_id,
        "presentation": {"kind": "tmux", "pane_id": snapshot.pane_id},
    }


async def managed_inventory(
    *, provider_id: str, generation: int, expected_server_identity: str
) -> tuple[dict[str, object], ...]:
    snapshots = await pane_inventory()
    if snapshots and snapshots[0].server_identity != expected_server_identity:
        raise TmuxError("tmux server identity changed")
    managed: list[dict[str, object]] = []
    for snapshot in snapshots:
        if snapshot.provider_id != provider_id:
            continue
        if not snapshot.managed:
            raise TmuxError("a provider-owned pane has incomplete terminal markers")
        if snapshot.dead:
            continue
        managed.append(terminal_identity(snapshot, provider_id=provider_id, generation=generation))
    if len(managed) > _MAX_TERMINALS:
        raise TmuxError("tmux provider inventory exceeds its bounded public limit")
    return tuple(managed)


async def create_terminal(
    *,
    provider_id: str,
    generation: int,
    participant_id: str,
    launch_id: str,
    executable: str,
    argv: Sequence[str],
    cwd: str,
    environment: Mapping[str, str],
    presentation: Mapping[str, object] | None,
    expected_server_identity: str,
    terminal_incarnation: str,
    provisional_window_name: str,
    dispatch_previously_started: bool,
    before_create: Callable[[], None] | None = None,
    ensure_usable: Callable[[], None] | None = None,
) -> dict[str, object]:
    existing = await _terminal_for_launch(provider_id, launch_id)
    if existing is not None:
        _require_reusable_terminal(
            existing,
            provider_id=provider_id,
            participant_id=participant_id,
            server_identity=expected_server_identity,
        )
        return terminal_identity(existing, provider_id=provider_id, generation=generation)
    created = await _recover_or_create_pane(
        provider_id=provider_id,
        generation=generation,
        expected_server_identity=expected_server_identity,
        provisional_window_name=provisional_window_name,
        dispatch_previously_started=dispatch_previously_started,
        executable=executable,
        argv=argv,
        cwd=cwd,
        environment=environment,
        presentation=presentation,
        before_create=before_create,
    )
    if ensure_usable is not None:
        ensure_usable()
    await mark_pane(
        created.pane_id,
        provider_id=provider_id,
        terminal_incarnation=terminal_incarnation,
        occupant_id=participant_id,
        pane_pid=created.pane_pid,
        launch_id=launch_id,
        executable=executable,
    )
    snapshot = await pane_snapshot(created.pane_id)
    if snapshot is None or snapshot.server_identity != expected_server_identity:
        raise TmuxError("created pane could not be verified on the pinned tmux server")
    if not exact_match(
        snapshot,
        server_identity=expected_server_identity,
        provider_id=provider_id,
        terminal_incarnation=terminal_incarnation,
        occupant_id=participant_id,
    ):
        raise TmuxError("created pane did not retain its terminal identity")
    name = _presentation_name(presentation)
    if name != provisional_window_name:
        if ensure_usable is not None:
            ensure_usable()
        await run("rename-window", "-t", snapshot.pane_id, name, check=False)
    return terminal_identity(snapshot, provider_id=provider_id, generation=generation)


async def _recover_or_create_pane(
    *,
    provider_id: str,
    generation: int,
    expected_server_identity: str,
    provisional_window_name: str,
    dispatch_previously_started: bool,
    executable: str,
    argv: Sequence[str],
    cwd: str,
    environment: Mapping[str, str],
    presentation: Mapping[str, object] | None,
    before_create: Callable[[], None] | None,
) -> PaneSnapshot:
    provisional = await _provisional_terminal(provisional_window_name)
    if provisional is not None:
        if (
            provisional.dead
            or provisional.server_identity != expected_server_identity
            or provisional.managed
        ):
            raise TmuxError("the recoverable launch pane no longer matches its intent")
        return provisional
    if dispatch_previously_started:
        raise TmuxOutcomeUnknown(
            "terminal creation was previously dispatched but no exact pane remains"
        )
    if (
        len(
            await managed_inventory(
                provider_id=provider_id,
                generation=generation,
                expected_server_identity=expected_server_identity,
            )
        )
        >= _MAX_TERMINALS
    ):
        raise TmuxError("tmux provider terminal limit reached")
    args = _new_window_args(
        session=(await _launch_session()),
        window_name=provisional_window_name,
        executable=executable,
        argv=argv,
        cwd=cwd,
        environment=environment,
        presentation=presentation,
    )
    if before_create is not None:
        before_create()
    pane_id = await run(*args)
    created = await pane_snapshot(pane_id)
    if created is None or created.server_identity != expected_server_identity:
        raise TmuxOutcomeUnknown("created pane could not be verified on the pinned tmux server")
    return created


async def recover_terminal_launch(
    *,
    provider_id: str,
    participant_id: str,
    launch_id: str,
    executable: str,
    terminal_incarnation: str,
    provisional_window_name: str,
    expected_server_identity: str,
    ensure_usable: Callable[[], None] | None = None,
) -> PaneSnapshot | None:
    existing = await _terminal_for_launch(provider_id, launch_id)
    if existing is not None:
        _require_reusable_terminal(
            existing,
            provider_id=provider_id,
            participant_id=participant_id,
            server_identity=expected_server_identity,
        )
        if (
            existing.terminal_incarnation != terminal_incarnation
            or existing.launch_executable != executable
        ):
            raise TmuxError("the recovered terminal changed its durable launch identity")
        return existing
    provisional = await _provisional_terminal(provisional_window_name)
    if provisional is None:
        return None
    if (
        provisional.dead
        or provisional.server_identity != expected_server_identity
        or provisional.managed
    ):
        raise TmuxError("the recoverable launch pane no longer matches its intent")
    if ensure_usable is not None:
        ensure_usable()
    await mark_pane(
        provisional.pane_id,
        provider_id=provider_id,
        terminal_incarnation=terminal_incarnation,
        occupant_id=participant_id,
        pane_pid=provisional.pane_pid,
        launch_id=launch_id,
        executable=executable,
    )
    recovered = await pane_snapshot(provisional.pane_id)
    if recovered is None or not exact_match(
        recovered,
        server_identity=expected_server_identity,
        provider_id=provider_id,
        terminal_incarnation=terminal_incarnation,
        occupant_id=participant_id,
    ):
        raise TmuxError("recoverable launch pane did not retain its terminal identity")
    return recovered


async def inspect_terminal(
    *,
    provider_id: str,
    generation: int,
    terminal_id: str,
    terminal_incarnation: str,
    expected_server_identity: str,
    screen_max_bytes: int = 0,
) -> tuple[dict[str, object], PresenceEvidence, str | None, bool]:
    snapshot = await pane_snapshot(terminal_id)
    if (
        snapshot is None
        or snapshot.provider_id != provider_id
        or snapshot.terminal_incarnation != terminal_incarnation
        or snapshot.server_identity != expected_server_identity
        or snapshot.occupant_id is None
        or snapshot.occupant_digest is None
        or snapshot.occupant_pane_pid != snapshot.pane_pid
        or snapshot.occupant_digest != occupant_digest(snapshot.occupant_id)
    ):
        raise TmuxError("terminal identity is absent or stale")
    if snapshot.dead:
        presence = PresenceEvidence("absent", "terminal_exited", None)
    else:
        if not exact_match(
            snapshot,
            server_identity=expected_server_identity,
            provider_id=provider_id,
            terminal_incarnation=terminal_incarnation,
            occupant_id=snapshot.occupant_id,
        ):
            raise TmuxError("terminal identity is absent or stale")
        presence = await observe_presence(snapshot)
    screen: str | None = None
    if screen_max_bytes:
        captured = await run("capture-pane", "-p", "-t", terminal_id, check=False)
        encoded = captured.encode("utf-8")[:screen_max_bytes]
        screen = encoded.decode("utf-8", "ignore")
    identity = terminal_identity(snapshot, provider_id=provider_id, generation=generation)
    return identity, presence, screen, not snapshot.dead


async def deliver_text(
    pane_id: str,
    text: str,
    *,
    enter: bool,
    before_effect: Callable[[], None] | None = None,
) -> None:
    buffer = f"{_BUFFER_PREFIX}{pane_id.lstrip('%')}-{secrets.token_hex(8)}"
    await run("load-buffer", "-b", buffer, "-", input_bytes=text.encode("utf-8"))
    try:
        paste = ["paste-buffer", "-b", buffer, "-t", pane_id, "-p", "-d"]
        if await _raw_paste_supported():
            paste.append("-S")
        if before_effect is not None:
            before_effect()
        await run(*paste)
    finally:
        await run("delete-buffer", "-b", buffer, check=False)
    if enter:
        await run("send-keys", "-t", pane_id, "Enter")


async def deliver_action(
    pane_id: str,
    action: Mapping[str, object],
    *,
    before_effect: Callable[[], None] | None = None,
) -> None:
    kind = action.get("kind")
    text = action.get("text")
    if not isinstance(text, str):
        raise TmuxError("terminal action text must be a string")
    if kind == "submit_text":
        await deliver_text(pane_id, text, enter=True, before_effect=before_effect)
    elif kind == "paste_text":
        await deliver_text(pane_id, text, enter=False, before_effect=before_effect)
    elif kind == "send_keys" and _SAFE_KEY.fullmatch(text):
        if before_effect is not None:
            before_effect()
        await run("send-keys", "-t", pane_id, text)
    else:
        raise TmuxError("terminal action is not a supported bounded tmux action")


async def interrupt_terminal(
    pane_id: str, action: object, *, before_effect: Callable[[], None] | None = None
) -> None:
    keys = {"interrupt": "C-c", "escape": "Escape", "signal": "C-c"}
    if not isinstance(action, str):
        raise TmuxError("terminal interrupt action is unsupported")
    key = keys.get(action)
    if key is None:
        raise TmuxError("terminal interrupt action is unsupported")
    if before_effect is not None:
        before_effect()
    await run("send-keys", "-t", pane_id, key)


async def terminate_terminal(
    snapshot: PaneSnapshot, *, before_effect: Callable[[], None] | None = None
) -> bool:
    current = await pane_snapshot(snapshot.pane_id)
    if current is None or current != snapshot:
        raise TmuxError("terminal identity changed before termination")
    if current.dead:
        return True
    if before_effect is not None:
        before_effect()
    await run("kill-pane", "-t", snapshot.pane_id)
    after = next(
        (pane for pane in await pane_inventory() if pane.pane_id == snapshot.pane_id), None
    )
    return after is None or after != snapshot


async def _terminal_for_launch(provider_id: str, launch_id: str) -> PaneSnapshot | None:
    matches = tuple(
        snapshot
        for snapshot in await pane_inventory()
        if snapshot.provider_id == provider_id and snapshot.launch_id == launch_id
    )
    if len(matches) > 1:
        raise TmuxError("multiple panes claim the same launch identity")
    return matches[0] if matches else None


async def _provisional_terminal(window_name: str) -> PaneSnapshot | None:
    if not _PROVISIONAL_WINDOW.fullmatch(window_name):
        raise TmuxError("recoverable launch window identity is invalid")
    output = await run(
        "list-panes",
        "-a",
        "-f",
        f"#{{==:#{{window_name}},{window_name}}}",
        "-F",
        "#{pane_id}",
    )
    pane_ids = tuple(value for value in output.splitlines() if value)
    if len(pane_ids) > 1:
        raise TmuxError("multiple panes claim the recoverable launch identity")
    return await pane_snapshot(pane_ids[0]) if pane_ids else None


async def _launch_session() -> str:
    sessions = sorted(
        session
        for session in (await run("list-sessions", "-F", "#{session_name}")).splitlines()
        if session
    )
    if not sessions:
        raise TmuxError("tmux provider has no pinned session")
    return sessions[0]


def _require_reusable_terminal(
    snapshot: PaneSnapshot,
    *,
    provider_id: str,
    participant_id: str,
    server_identity: str,
) -> None:
    if snapshot.dead:
        raise TmuxError("the prior launch terminal has already exited")
    if snapshot.server_identity != server_identity:
        raise TmuxError("the prior launch belongs to another tmux server")
    if snapshot.occupant_id != participant_id or not exact_match(
        snapshot,
        server_identity=server_identity,
        provider_id=provider_id,
        terminal_incarnation=str(snapshot.terminal_incarnation),
        occupant_id=participant_id,
    ):
        raise TmuxError("the launch marker belongs to another participant")


def _new_window_args(
    *,
    session: str,
    window_name: str,
    executable: str,
    argv: Sequence[str],
    cwd: str,
    environment: Mapping[str, str],
    presentation: Mapping[str, object] | None,
) -> list[str]:
    if not argv or argv[0] != executable:
        raise TmuxError("launch argv must preserve its declared executable")
    if any("\x00" in value for value in (executable, cwd, *argv)):
        raise TmuxError("launch values contain a NUL byte")
    background = (presentation or {}).get("background", True)
    if type(background) is not bool:
        raise TmuxError("launch presentation.background must be a boolean")
    args = [
        "new-window",
        "-P",
        "-F",
        "#{pane_id}",
        "-t",
        f"{session}:",
        "-n",
        window_name,
        "-c",
        cwd,
    ]
    if background:
        args.insert(1, "-d")
    for key, value in environment.items():
        if not key or "=" in key or any(character in key + value for character in "\x00\r\n"):
            raise TmuxError("launch environment contains an invalid entry")
        args.extend(("-e", f"{key}={value}"))
    args.extend(("--", *argv))
    return args


def _presentation_name(presentation: Mapping[str, object] | None) -> str:
    value = (presentation or {}).get("name", "theater")
    return value[:256] if isinstance(value, str) and value else "theater"


async def _raw_paste_supported() -> bool:
    if _VERSION_CACHE[0] is _UNPROBED:
        output = await run("-V", check=False)
        match = _VERSION_PATTERN.search(output)
        _VERSION_CACHE[0] = (
            tuple(int(part) for part in match.group().split(".")) if match is not None else None
        )
    version = _VERSION_CACHE[0]
    if version is None or not isinstance(version, tuple):
        return False
    target = (3, 7)
    padded = version + (0,) * max(0, len(target) - len(version))
    return padded[: len(target)] >= target


__all__ = [
    "create_terminal",
    "deliver_action",
    "ensure_server",
    "inspect_terminal",
    "interrupt_terminal",
    "managed_inventory",
    "recover_terminal_launch",
    "terminal_identity",
    "terminate_terminal",
]
