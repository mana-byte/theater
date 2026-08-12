"""The régie Textual application.

Layout:
    ┌──────────────────────┬──────────────────────────┐
    │ tree (top)           │  stage                   │
    │                      │  (tmux pane, not ours)   │
    │ bus (bottom)         │                          │
    └──────────────────────┴──────────────────────────┘

The tree and bus are Textual widgets. The stage is not a widget — it is a
real tmux pane in the same window. The régie is itself a pane in that window,
and the stage occupies the space next to it. When the user selects an agent,
the daemon tells tmux to join that agent's pane into the stage window; the
régie then re-asserts its own pane layout so the tree stays visible.

Keybindings:
    j/k or up/down  navigate the tree
    Enter           stage the selected agent (join its pane into this window)
    z               focus the staged pane (type at the agent directly)
    x               kill the selected agent's pane
    q               quit (unstages first; detaches, kills nothing)

Polling: the tree refreshes every 1s, the bus tail every 0.4s. Both are
async daemon calls; the app runs them as background workers.
"""

from __future__ import annotations

import logging

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.reactive import reactive
from textual.widgets import Footer, Label, RichLog
from rich.text import Text

from theater.client import DaemonClient
from theater.regie.bus_view import format_bus_line
from theater.regie.tree import render_tree, selected_participant
from theater.tmux import client as tmux
from theater.tmux import panes

logger = logging.getLogger("theater.regie")

#: How often to refresh the participant tree (seconds).
TREE_INTERVAL = 1.0

#: How often to poll the bus for new events (seconds).
BUS_INTERVAL = 0.4

#: How many bus events to pull per poll.
BUS_BATCH = 50


class TreePanel(VerticalScroll):
    """A scrollable list of participant tree lines.

    Each line is a separate Label widget, so the panel scrolls natively when
    the list is longer than the viewport. Cursor and staged highlighting are
    done via Textual CSS classes (which respect the user's theme) rather than
    hardcoded Rich colors.
    """

    lines: reactive[list[tuple[Text, dict]]] = reactive([])

    DEFAULT_CSS = """
    TreePanel {
        height: 1fr;
        scrollbar-size: 1 1;
    }
    TreePanel > Label {
        height: 1;
        padding: 0 1;
        margin: 0 0;
    }
    TreePanel > Label.tree-cursor {
        background: $accent 20%;
        text-style: bold;
    }
    TreePanel > Label.tree-staged {
        background: $primary 20%;
    }
    TreePanel > Label.tree-cursor.tree-staged {
        background: $accent 30%;
        text-style: bold;
    }
    """

    def watch_lines(self, lines: list[tuple[Text, dict]]) -> None:
        """Rebuild the widget children when lines change."""
        self.remove_children()
        if not lines:
            self.mount(Label(Text("no participants", style="dim italic")))
            return
        widgets = [Label(label) for label, _ in lines]
        self.mount(*widgets)

    def apply_cursor(self, cursor: int, staged_pane: str | None) -> None:
        """Add CSS classes to the cursor and staged lines, remove from others."""
        for i, child in enumerate(self.children):
            child.remove_class("tree-cursor")
            child.remove_class("tree-staged")
            # Need to find the node for this line to check staged
            if i < len(self._lines_data):
                _, node = self._lines_data[i]
                if staged_pane and node.get("tmux_pane") == staged_pane:
                    child.add_class("tree-staged")
            if i == cursor:
                child.add_class("tree-cursor")

    #: Set by the app so apply_cursor can map line index to node.
    _lines_data: list[tuple[Text, dict]] = []

    def scroll_to_cursor(self, cursor: int) -> None:
        """Ensure the cursor line is visible."""
        if cursor < 0 or cursor >= len(self.children):
            return
        try:
            child = self.children[cursor]
            self.scroll_to_widget(child)
        except Exception:
            pass


class RegieApp(App):
    """The theater control panel."""

    CSS = """
    Screen {
        layout: horizontal;
    }
    #sidebar {
        width: 52;
        layout: vertical;
        border: round $primary;
    }
    #tree-panel {
        height: 1fr;
    }
    #bus-panel {
        height: 12;
        border: round $accent;
    }
    .log {
        background: $surface;
    }
    """

    BINDINGS = [
        Binding("j", "cursor_down", "down", show=False),
        Binding("k", "cursor_up", "up", show=False),
        Binding("down", "cursor_down", "down", show=False),
        Binding("up", "cursor_up", "up", show=False),
        Binding("enter", "stage", "stage"),
        Binding("z", "focus_stage", "focus"),
        Binding("x", "kill", "kill"),
        Binding("q", "quit", "quit"),
    ]

    title = "theater régie"

    cursor: reactive[int] = reactive(0)
    tree_lines: reactive[list[tuple[Text, dict]]] = reactive([])
    bus_cursor: int = 0
    #: The régie's own pane id (from $TMUX_PANE), discovered at mount.
    my_pane: str | None = None
    #: The window id the régie lives in, discovered at mount.
    my_window: str | None = None
    #: The pane id currently on stage (joined into our window), or None.
    staged_pane: reactive[str | None] = reactive(None)
    #: The session the régie is running in. tmux options are scoped to it.
    my_session: str | None = None
    #: The session-local value of tmux's `mouse` option before we changed it,
    #: or None if the session had no override of its own.
    _mouse_prev: str | None = None
    #: Whether we actually changed the option, so a failed enable does not
    #: cause a restore that clobbers a setting we never touched.
    _mouse_set: bool = False
    #: Teardown runs from two places and must not run twice.
    _torn_down: bool = False

    def __init__(self):
        super().__init__()
        self._client: DaemonClient | None = None

    def compose(self) -> ComposeResult:
        # No Header: it spent a whole row restating the app's own class name to
        # someone who just typed the command that started it. The footer already
        # carries the keybindings, which is the only thing worth the space.
        with Vertical(id="sidebar"):
            yield TreePanel(id="tree-panel")
            yield RichLog(id="bus-panel", max_lines=200, wrap=False, markup=True)
        yield Footer()

    async def on_mount(self) -> None:
        self._client = DaemonClient()
        await self._client.connect()
        # Discover our own pane and window id. The régie is itself a tmux
        # pane — staging means joining another pane into this window, so we
        # need to know which window we are in.
        my_pane = tmux.current_pane()
        if my_pane:
            self.my_pane = my_pane
            try:
                self.my_window = await tmux.display_message(
                    "#{window_id}", target=my_pane
                )
                self.my_session = await tmux.display_message(
                    "#{session_id}", target=my_pane
                )
            except Exception as exc:
                logger.debug("could not discover window/session id: %s", exc)
        await self._enable_mouse()
        self.set_interval(TREE_INTERVAL, self._refresh_tree)
        self.set_interval(BUS_INTERVAL, self._refresh_bus)
        await self._refresh_tree()
        await self._refresh_bus()

    async def on_unmount(self) -> None:
        # Best effort only. Exits that go through `q` or ctrl-c have already
        # torn down in action_quit, where awaiting still works; this catches
        # the paths that never reach it.
        await self._teardown()
        if self._client:
            await self._client.aclose()

    # ---- tmux lifecycle -------------------------------------------------

    async def _enable_mouse(self) -> None:
        """Turn tmux mouse reporting on for the régie's session.

        Scoped to the session rather than `-g`: a global set would change every
        session on the server and outlive this process, which is not a choice a
        TUI gets to make on the user's behalf. The previous session-local value
        is remembered so exiting puts it back.
        """
        if not self.my_session:
            return
        try:
            self._mouse_prev = await tmux.show_option("mouse", target=self.my_session)
            await tmux.set_option("mouse", "on", target=self.my_session)
            self._mouse_set = True
        except Exception as exc:
            logger.debug("could not enable mouse: %s", exc)

    async def _restore_mouse(self) -> None:
        if not self._mouse_set or not self.my_session:
            return
        self._mouse_set = False
        try:
            if self._mouse_prev is None:
                # The session had no override before us, so remove ours rather
                # than pinning it to whatever the global value happened to be.
                await tmux.unset_option("mouse", target=self.my_session)
            else:
                await tmux.set_option(
                    "mouse", self._mouse_prev, target=self.my_session
                )
        except Exception as exc:
            logger.debug("could not restore mouse: %s", exc)

    async def _teardown(self) -> None:
        """Leave tmux as we found it: nothing staged, options restored.

        Unstaging matters more than it looks. The staged agent's pane lives in
        the régie's window only for as long as the régie is there to frame it;
        quitting without breaking it out would leave the agent alive but sharing
        a window with a dead TUI. `break_pane` moves it back to a window of its
        own without touching the process.
        """
        if self._torn_down:
            return
        self._torn_down = True
        pane = self.staged_pane
        if pane:
            try:
                await panes.break_pane(pane)
            except Exception as exc:
                logger.debug("unstage on exit failed: %s", exc)
        await self._restore_mouse()

    async def action_quit(self) -> None:
        """Quit, but put the stage back first.

        This has to happen here and not only in `on_unmount`: by unmount time
        Textual is already shutting the loop down, and an awaited tmux call can
        be cancelled halfway through — which would strand the staged agent in a
        window whose other occupant has exited.
        """
        await self._teardown()
        self.exit()

    # ---- polling -------------------------------------------------------

    async def _refresh_tree(self) -> None:
        if not self._client:
            return
        try:
            tree = await self._client.call("participants.tree")
            assert isinstance(tree, list)
            unmanaged = await self._client.call("participants.unmanaged")
            assert isinstance(unmanaged, list)
        except Exception as exc:
            logger.debug("tree refresh failed: %s", exc)
            return
        self.tree_lines = render_tree(tree, unmanaged)
        # Clamp cursor
        if self.cursor >= len(self.tree_lines):
            self.cursor = max(0, len(self.tree_lines) - 1)
        self._render_tree()

    async def _refresh_bus(self) -> None:
        if not self._client:
            return
        try:
            rows = await self._client.call(
                "bus.tail", limit=BUS_BATCH, after_id=self.bus_cursor
            )
            assert isinstance(rows, list)
        except Exception as exc:
            logger.debug("bus refresh failed: %s", exc)
            return
        if not rows:
            return
        # bus.tail returns newest N after cursor (DESC), so we need to
        # reverse for display if there's a gap
        if rows[0]["id"] > self.bus_cursor + 1 and self.bus_cursor > 0:
            missed = rows[0]["id"] - self.bus_cursor - 1
            log = self.query_one("#bus-panel", RichLog)
            log.write(Text(f"... {missed} events dropped", style="dim italic"))
        # Sort ascending for display
        rows_sorted = sorted(rows, key=lambda r: r["id"])
        log = self.query_one("#bus-panel", RichLog)
        for row in rows_sorted:
            log.write(format_bus_line(row))
        self.bus_cursor = rows[-1]["id"]

    # ---- rendering -----------------------------------------------------

    def _panel(self) -> TreePanel | None:
        """The tree panel, or None when there is no mounted widget to draw on.

        Reactive watchers fire on assignment, which can happen before mount and
        during teardown as well as in the middle of a normal frame. A missing
        widget in those windows is expected, not an error.
        """
        try:
            return self.query_one("#tree-panel", TreePanel)
        except Exception:
            return None

    def _render_tree(self) -> None:
        panel = self._panel()
        if panel is None:
            return
        panel._lines_data = self.tree_lines
        panel.lines = self.tree_lines
        panel.apply_cursor(self.cursor, self.staged_pane)
        panel.scroll_to_cursor(self.cursor)

    def watch_cursor(self, cursor: int) -> None:
        """Redraw the tree with the cursor highlighted."""
        panel = self._panel()
        if panel is None:
            return
        panel.apply_cursor(cursor, self.staged_pane)
        panel.scroll_to_cursor(cursor)

    def watch_staged_pane(self, _pane: str | None) -> None:
        """Redraw the tree so the staged pane gets its background."""
        panel = self._panel()
        if panel is None:
            return
        panel.apply_cursor(self.cursor, self.staged_pane)

    # ---- actions -------------------------------------------------------

    def action_cursor_down(self) -> None:
        if self.cursor < len(self.tree_lines) - 1:
            self.cursor += 1
            self._render_tree()

    def action_cursor_up(self) -> None:
        if self.cursor > 0:
            self.cursor -= 1
            self._render_tree()

    async def action_stage(self) -> None:
        """Stage the selected agent: join its pane into the régie's window.

        If something is already staged, break it back out to a hidden window
        first. Then join the new pane in and resize it to fill the stage area.
        """
        node = selected_participant(self.tree_lines, self.cursor)
        if node is None:
            self.notify("nothing to stage", severity="warning")
            return
        pane = node.get("tmux_pane")
        if not pane:
            self.notify(f"{node.get('id', '?')[:8]} has no pane", severity="warning")
            return
        if not self.my_window:
            self.notify("régie window not discovered — cannot stage", severity="error")
            return

        # Already staged? Unstage first.
        if self.staged_pane and self.staged_pane != pane:
            try:
                await panes.break_pane(self.staged_pane)
                self.notify(f"unstaged {self.staged_pane}")
            except Exception as exc:
                logger.debug("unstage failed: %s", exc)

        if self.staged_pane == pane:
            # Toggle: unstaging the current occupant
            try:
                await panes.break_pane(pane)
                self.staged_pane = None
                self.notify(f"unstaged {node.get('harness', '?')} ({pane})")
            except Exception as exc:
                self.notify(f"unstage failed: {exc}", severity="error")
            return

        # Join the agent's pane into our window.
        try:
            await panes.join_pane(pane, target_window=self.my_window)
            self.staged_pane = pane
            # Resize the *régie* pane down to sidebar width; tmux
            # automatically gives the rest to the staged pane. This is
            # more reliable than resizing the staged pane to a calculated
            # width, because it does not depend on knowing the window's
            # actual column count.
            if self.my_pane:
                try:
                    await panes.resize_pane(self.my_pane, width=52)
                except Exception as exc:
                    logger.debug("resize after stage failed: %s", exc)
            self.notify(f"staged {node.get('harness', '?')} ({pane})")
        except Exception as exc:
            self.notify(f"stage failed: {exc}", severity="error")

    async def action_kill(self) -> None:
        """Kill the selected participant."""
        node = selected_participant(self.tree_lines, self.cursor)
        if node is None:
            self.notify("nothing to kill", severity="warning")
            return
        pid = node.get("id")
        if not pid:
            self.notify("cannot kill an unmanaged pane", severity="warning")
            return
        if not self._client:
            return
        try:
            await self._client.call("participant.kill", id=pid)
            self.notify(f"killed {pid[:8]}")
        except Exception as exc:
            self.notify(f"kill failed: {exc}", severity="error")
        await self._refresh_tree()

    async def action_focus_stage(self) -> None:
        """Focus the staged pane so the user can type at the agent.

        This is the 'zoom' action — it selects the staged pane in tmux,
        which brings it to the foreground of the window. The user can then
        interact with the agent directly. Pressing q in the régie still
        detaches everything.
        """
        if not self.staged_pane:
            self.notify("nothing staged — press Enter to stage first", severity="warning")
            return
        try:
            await panes.select_pane(self.staged_pane)
            self.notify(f"focusing {self.staged_pane} — press Ctrl-B o to return to régie")
        except Exception as exc:
            self.notify(f"focus failed: {exc}", severity="error")

    # ---- tree rendering with cursor ------------------------------------

    # (highlighting is now done via Textual CSS classes in apply_cursor,
    #  not by rewriting Rich Text styles)


def run_regie() -> None:
    """Entry point for `theater regie`."""
    app = RegieApp()
    app.run()
