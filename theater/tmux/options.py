"""Session-scoped tmux options and key bindings.

Session scope on purpose: ``set-option -g`` would rewrite the user's tmux
behaviour for every session and outlive this process. ``unset_option`` on
the way out keeps the blast radius to the session the régie runs in.
"""

from __future__ import annotations

from theater.tmux.command import _FORMAT_SEP


async def show_option(name: str, *, target: str) -> str | None:
    """The session-local value of an option, or None if it is not set there.

    Deliberately not ``-g``: the question is "did this session override the
    option", because that is what has to be put back afterwards. An unset
    option prints nothing, which is distinguishable from the value "off".
    """
    from theater.tmux.client import run

    out = await run("show-options", "-t", target, name, check=False)
    if not out.strip():
        return None
    # Output is "<name> <value>"; treat anything else as unset rather than guess.
    parts = out.split(None, 1)
    return parts[1].strip() if len(parts) == 2 else None


async def set_option(name: str, value: str, *, target: str) -> None:
    from theater.tmux.client import run

    await run("set-option", "-t", target, name, value)


async def unset_option(name: str, *, target: str) -> None:
    """Drop a session-local override so the global value applies again."""
    from theater.tmux.client import run

    await run("set-option", "-u", "-t", target, name, check=False)


# ---- key bindings (`#{key}` expands to empty; `#{key_string}` holds the name)


async def key_bound(table: str, key: str) -> bool:
    """Whether *key* already has a binding in *table*.

    Checked, not ``check=False``: a failed inspection must raise rather than
    read as "nothing bound", or ``bind_key_if_free`` would overwrite a binding
    it never actually saw.
    """
    from theater.tmux.client import run

    out = await run("list-keys", "-T", table, "-F", "#{key_string}")
    return key in out.splitlines()


async def bind_key_if_free(table: str, key: str, command: list[str], *, note: str) -> bool:
    """Bind *key* in *table* to *command*, tagged with *note*, unless already bound.

    Returns whether the bind happened, so the caller knows whether it owns
    the key and should remove it on teardown.
    """
    from theater.tmux.client import key_bound, run

    if await key_bound(table, key):
        return False
    await run("bind-key", "-T", table, "-N", note, key, *command)
    return True


async def unbind_key_if_owned(table: str, key: str, *, note: str) -> None:
    """Remove *key* from *table*, but only if its note still matches *note*.

    Guards against removing a binding the user made after ours (e.g. a
    config reload). *note* is shared by every caller using the same value,
    so it does not distinguish between two concurrent callers racing on
    the same key — only between "ours" and "not ours".
    """
    from theater.tmux.client import run

    out = await run(
        "list-keys", "-T", table, "-F", f"#{{key_string}}{_FORMAT_SEP}#{{key_note}}", check=False
    )
    for line in out.splitlines():
        bound_key, _, bound_note = line.partition(_FORMAT_SEP)
        if bound_key == key and bound_note == note:
            await run("unbind-key", "-T", table, key, check=False)
            return
