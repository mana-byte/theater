"""OpenAI Codex CLI.

Launch lever
------------
`-c key=value` sets a config override, and the value is parsed as TOML, so the
MCP server is registered by writing three dotted keys inline:

    -c mcp_servers.theater.command="…"  -c mcp_servers.theater.args=["mcp",…]

Verified with `codex mcp list` inside a launched session. This is an *override*
on top of ~/.codex/config.toml rather than a replacement, so the user's own
servers survive — same policy as the other two adapters.

Approval flags are always passed in pairs (`-a` with `-s`). Codex has two
independent axes — approval policy and sandbox — and with neither flag it
inherits whatever the user put in ~/.codex/config.toml, which may well be
`never` / `danger-full-access`. Theater's approval mode is a promise to the
caller of `spawn`, so it must not be inheritable.

The first-launch trust dialog
-----------------------------
On the first launch inside a directory that is not listed under
`[projects."<path>"] trust_level = "trusted"` in ~/.codex/config.toml, codex
shows a modal asking whether you trust the directory, and nothing runs until a
human answers. Tested and unable to suppress: `-a untrusted -s read-only`,
`--dangerously-bypass-approvals-and-sandbox`, and both spellings of a
`-c projects."…".trust_level="trusted"` override. Two ways out were considered
and rejected — writing the trust entry into the user's config (Theater does not
own that file) and pointing CODEX_HOME elsewhere (loses auth.json, the user's
MCP servers, and session history). So a spawn into a fresh directory sits at the
dialog until someone answers it once. `is_idle_screen` reports that pane as
awaiting input because the trust dialog renders a `›` selection row, which
is the same glyph the idle composer uses. `screen_reading` checks the modal
markers before falling through to `is_idle_screen`, so the trust dialog
classifies as TRUST rather than PROMPT.

Transcript layout
-----------------
    ~/.codex/sessions/<YYYY>/<MM>/<DD>/rollout-<local-ISO>-<session_id>.jsonl

Two independent traps in that name. The timestamp is *local* time while every
`timestamp` field inside the file is UTC, so the two are never comparable — time
filtering here goes through stat() and nothing else. The uuid suffix, on the
other hand, is exactly `session_meta.payload.session_id` (checked on every
transcript to hand), which makes a known session id a pure glob.

Which rollout is ours
---------------------
Codex mints its `ThreadId` internally and the public CLI accepts a session id
only on `resume` and `fork`, so a new interactive session cannot be launched
with an id we chose. Until a transcript is found, the participant therefore has
no session id at all, and discovery has nothing sharper than `session_meta.cwd`
plus a birth-time floor. Two agents in one directory both satisfy that, so the
reducer's collision guard refuses both — correctly, and at the cost of the
await and of `read_transcript`.

The exact channel is the process itself: codex holds its rollout open for the
lifetime of the session, so the file descriptors of the pane's codex process
name the transcript that belongs to it. That evidence survives a daemon
restart, is available before the agent has made a single MCP call, and changes
no Codex configuration — which is more than any of `CODEX_HOME` isolation, a
`SessionStart` hook receipt, or `_meta.threadId` on an MCP request can say.

It applies to spawned panes only. There the pane process *is* the CLI Theater
started, so the pid the registry holds names that session for as long as the
participant lives. An adopted pane runs a shell instead, and a shell outlives
what it ran: the codex under it now need not be the codex the participant was
adopted from, and no amount of counting processes can tell the difference. So
adopted panes get no proof and keep the behaviour they have always had. Giving
them proof means associating a participant with a *process* at adoption time
and keeping it — daemon state, and the daemon's to keep.

Three keys, then, in a deliberate order. A session id we were *given* — a
resume token, a launch receipt — is asked first: it names the file outright,
no second codex in the pane can confuse it, and it costs a glob instead of
three subprocesses. The process is asked next. A session id we merely *read
back* off a file comes last, behind the process, because it may itself be an
earlier guess: put it first and discovery re-derives the same wrong file
forever, with no way for proof to ever displace it.

When the process cannot be inspected — no `/proc`, no `lsof`, a rollout not
yet created, more than one open at once, or more than one codex in the pane to
choose between — discovery falls back to the cwd scan exactly as before and the
candidate is reported as heuristic. Nothing here decides what to do about that:
the reducer's guard is the one place that refuses a contested attachment, and
this adapter's job is only to say honestly how well it knows.

Proof is also offered on its own, through `proven_transcript`. A participant
bound before any of this existed carries a heuristic location that every later
poll takes before discovery is consulted, so it would stay contested for the
rest of its life; the source offers such a location to the proof channel, and
only to the proof channel, so a failed probe leaves it alone rather than
replacing it with a fresh guess.

Record shape
------------
One JSON record per line, `{timestamp, type, payload}`, discriminated on
`payload.type`. The turn boundary is `task_complete`, and its
`last_agent_message` repeats the final `agent_message` verbatim — which is why
the `final_answer` phase is dropped and the text is taken from the boundary
record instead. The observer hands the *turn-ending* event's text back to
whoever is awaiting the job (observer `_answer_turn`), so a boundary event with
no text would resolve the send with an empty result — the observer falls back
to the turn's last assistant text for exactly this shape, but a boundary that
carries its own text is better than relying on that.

`turn_aborted` (a human pressing esc) also ends the turn. It has to: 8
`task_started` records across the sampled transcripts closed as 5
`task_complete` plus 3 `turn_aborted`, and treating the aborts as non-terminal
would leave a caller awaiting a reply that is never coming.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path

from theater import proc
from theater.harness.base import (
    APPROVALS,
    SERVER_NAME,
    Event,
    EventKind,
    EventPath,
    Harness,
    LaunchPlan,
    NativeChild,
    clipper,
    theater_binary,
)
from theater.harness.observation import (
    ScreenConfidence,
    ScreenKind,
    ScreenReading,
    TranscriptObserver,
)
from theater.harness.source import Source, TranscriptCandidate, TranscriptSource
from theater.models import BadRequest
from theater.provenance import TranscriptProvenance, normalize_provenance

logger = logging.getLogger("theater.harness.codex")

#: The composer prompt. A single glyph (U+203A), not the ASCII ">" that
#: Claude Code uses.
PROMPT = "\u203a"

#: Present in the status bar for as long as a turn is running. Codex keeps a
#: persistent footer under the composer, so the bottom line is never the
#: prompt and `last_screen_line` cannot be used.
WORKING_MARKER = "esc to interrupt"

#: Rendered by the approval overlay, the MCP elicitation prompt, and the auth
#: prompt — all three are awaiting-input screens. NOT `to confirm`: the
#: `/approvals` settings popup renders `to confirm or … to go back`, and
#: keying on `to confirm` would classify that popup as an approval modal. The
#: substring is loose for keymap-independence (only the key glyph varies), and
#: safe only because of TWO guards together: (1) scoped to the tail window via
#: `_in_screen_tail`, and (2) an `endswith` test, not containment. The tail
#: window unavoidably contains agent output (three of five lines in a real
#: codex idle pane are prose), and prose can contain the phrase mid-line. The
#: footer is a whole line that ends with the marker; prose virtually never
#: does. Both guards are required; do not drop either.
APPROVAL_MARKER = "to cancel"

#: The first-launch trust dialog. The full sentence is longer, but the
#: paragraph wraps mid-sentence on panes narrower than ~46 columns, so only
#: the first few words are reliable. A whole-capture match, not tail-scoped:
#: the trust paragraph is body text above the selection rows, so tail-scoping
#: would miss it. Safe because the trust dialog only appears at startup, when
#: there is no agent output on the pane.
TRUST_MARKER = "Do you trust the contents"

#: How far up from the bottom to look for the composer. The footer is one
#: line, but a multi-line composer or notice can push the prompt further up.
_SCREEN_TAIL_LINES = 5

#: `session_meta` is the first record and carries `cwd`, so a candidate is
#: probed by reading exactly one line. Observed at 18-22 KB (the payload embeds
#: the whole system prompt), so the cap only exists to stop a pathological file
#: from being read into memory.
_CWD_PROBE_BYTES = 256 * 1024

#: The filename is `rollout-<local ISO with - separators>-<uuid>`. Anchoring on
#: the fixed-width timestamp is what lets the uuid keep its own hyphens.
_STEM = re.compile(r"^rollout-\d{4}-\d\d-\d\dT\d\d-\d\d-\d\d-(.+)$")

#: apply_patch hunks are delimited by these markers, one per line. The paths
#: that follow them are repo-relative — codex's own parser
#: (apply-patch/src/parser.rs:39-41) treats them as relative to the session
#: cwd, so no relativisation is needed. `*** End Patch` is the terminator.
_PATCH_FILE_RE = re.compile(r"^\*\*\* (?:Update|Add|Delete) File: (.+)$", re.MULTILINE)


def _apply_patch_paths(text: str) -> tuple[EventPath, ...]:
    """Extract file paths from an ``apply_patch`` tool input string.

    The apply_patch format is a structured patch grammar with explicit
    per-file markers (``*** Update File:``, ``*** Add File:``, ``*** Delete
    File:``), not prose or a shell command. The markers and their grammar are
    defined in codex-rs/apply-patch/src/parser.rs:39-41. Every hunk is a write
    — update, create, and delete are all mutations — so every path gets
    ``mode="write"``.

    A malformed input yields nothing rather than a partial guess: a wrong
    path in the touch index is worse than a missing one.
    """
    if not isinstance(text, str):
        return ()
    return tuple(
        EventPath(path=match.strip(), mode="write") for match in _PATCH_FILE_RE.findall(text)
    )


def _in_screen_tail(capture: str, marker: str) -> bool:
    """Whether any of the last few non-blank lines *ends with* *marker*.

    The approval footer is chrome the CLI always draws at the bottom of the
    modal, so searching the whole pane buys nothing — and matching the whole
    pane lets agent output (ordinary prose) impersonate the footer. Scoping
    to the same tail window ``is_idle_screen`` uses is necessary but not
    sufficient on its own: a real codex idle pane has agent output in three
    of the five scanned tail lines (see ``codex_idle.txt``), so the window
    unavoidably contains prose. The end-of-line anchor is the second guard:
    the footer is a whole line that ends with the marker, while prose
    containing the phrase virtually never ends a line with it. Neither the
    tail window nor the endswith test alone is enough; both are required.
    """
    lines = [line.strip() for line in capture.splitlines() if line.strip()]
    return any(line.endswith(marker) for line in lines[-_SCREEN_TAIL_LINES:])


def _resolve(path: Path) -> Path:
    """`Path.resolve`, but a path we cannot stat is not an error here.

    Every comparison in the correlation path is between a name the kernel gave
    us and a name a human configured, and on macOS those differ by `/private`
    for anything under a temporary directory. Resolving both sides is what
    makes them comparable; a file that vanished mid-probe just compares as
    itself.
    """
    try:
        return path.resolve()
    except OSError:
        return path


def _is_codex(comm: str) -> bool:
    """Whether a `ps` command column names the codex CLI.

    Compared on the basename: `ps -o comm` gives a bare `codex` for a plain
    install and an absolute path for some wrappers, and under Nix the image
    behind it is a `.codex-wrapped` shim the column never shows.
    """
    return comm.rsplit("/", 1)[-1] == CodexHarness.binary


def _epoch(value) -> float | None:
    """Codex writes ISO-8601 with a Z suffix, same as Claude Code."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _flatten(output) -> str:
    """Tool output is a list of `{"type": "input_text", "text": …}` blocks."""
    if isinstance(output, str):
        return output
    if not isinstance(output, list):
        return "" if output is None else json.dumps(output, default=str)
    parts = []
    for block in output:
        if isinstance(block, dict) and isinstance(block.get("text"), str):
            parts.append(block["text"])
        elif isinstance(block, str):
            parts.append(block)
    return "".join(parts)


def _turn_id(payload: dict) -> str | None:
    """The turn this record belongs to, as Codex names it.

    Stamped identically on `task_started` and on whichever record closes the
    turn, so the two ends of a turn are joinable without inference. Only read
    off the boundary records: the mid-turn `agent_message` and `user_message`
    events carry no turn_id at all, and inventing one for them by remembering
    the last `task_started` would mean holding state across lines, which
    parse() deliberately does not do.
    """
    tid = payload.get("turn_id")
    return tid if isinstance(tid, str) and tid else None


class CodexHarness(Harness):
    name = "codex"
    binary = "codex"
    #: A filled ring. Not another asterisk-family glyph: `✻` is taken by Claude
    #: Code and the near-neighbours (`✳ ❋ ✺`) are hard to tell apart.
    icon = "\u25c9"
    #: A spelling that does not normalize is observed as nothing at all, so
    #: these are not cosmetic.
    aliases = ("codex-cli", "codex_cli", "openai-codex", "Codex")

    def __init__(self, root: Path | None = None):
        #: The observer's business alone; nothing about launching depends on it.
        self.observer = CodexObserver(root=root)

    # ---- launching ------------------------------------------------------

    def plan_launch(
        self,
        *,
        participant_id: str,
        prompt: str,
        config_path: Path,
        approval: str,
        model: str | None = None,
        resume: str | None = None,
    ) -> LaunchPlan:
        if approval not in APPROVALS:
            raise BadRequest(f"approval must be one of {', '.join(APPROVALS)}, got {approval!r}")
        command = json.dumps(theater_binary())
        args = json.dumps(["mcp", "--id", participant_id])
        # `codex resume <SESSION_ID>` is a subcommand (cli/src/main.rs:181-182,
        # 315-339), not a flag. It shares the same `-c` overrides and approval
        # flags via the `SessionTuiCli` wrapper (main.rs:403), a newtype over
        # TuiCli whose only structural difference is the `resume` token and
        # session id positional.
        argv = [
            "codex",
        ]
        if resume is not None:
            argv.append("resume")
            argv.append(resume)
        argv += [
            "-c",
            f"mcp_servers.{SERVER_NAME}.command={command}",
            "-c",
            f"mcp_servers.{SERVER_NAME}.args={args}",
        ]
        if model:
            argv += ["--model", model]
        if approval == "yolo":
            argv.append("--dangerously-bypass-approvals-and-sandbox")
        elif approval == "edits":
            argv += ["-a", "on-request", "-s", "workspace-write"]
        else:
            argv += ["-a", "untrusted", "-s", "read-only"]
        if prompt:
            argv.append(prompt)
        return LaunchPlan(argv=argv, session_id=resume)


class _CodexSource(TranscriptSource):
    """A codex transcript source whose exactness is decided per location.

    The flags `TranscriptSource` already understands are fixed when the source
    is built: either every candidate under this root has one owner, or the
    session id we were handed was itself exact. Neither describes codex, where
    the same source proves ownership on one poll — the process was holding the
    file — and can only guess on the next, because `lsof` is missing or the
    rollout does not exist yet. So the question is asked about the path.
    """

    def __init__(self, observer: CodexObserver, **kwargs) -> None:
        super().__init__(observer, **kwargs)
        #: The same object as `self._observer`, kept under its own name so the
        #: codex-only `proved` call does not read as a `TranscriptObserver` API.
        self._codex = observer

    def correlation_for(self, path: Path, session_id: str | None) -> str:
        if self._codex.proved(path):
            return str(TranscriptProvenance.PROVEN)
        return super().correlation_for(path, session_id)

    def commit_attachment(self) -> None:
        super().commit_attachment()
        # One fact, held in two places: the source's flag labels the answer,
        # the observer's decides which key discovery asks first. Committing a
        # guessed location clears the first, and leaving the second set would
        # send the next lookup to that id's glob ahead of the process — which
        # is the ordering that cannot be corrected.
        #
        # Defence in depth rather than a live guard: today a committed location
        # is taken before discovery runs, a revoked one clears the id, and a
        # source rebuilt from the registry is given heuristic provenance — so
        # there is no path on which the stale flag is currently read. Keeping
        # the two in step costs one line and removes the need to re-derive that
        # every time one of those three changes.
        self._codex._session_exact = self._session_provenance is TranscriptProvenance.EXACT


class CodexObserver(TranscriptObserver):
    """Read `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`.

    Note the date directories: rollout files are filed under the day the
    session started, in UTC, which is not the local date for most of the world
    for part of every day.
    """

    #: The process holds its rollout open, so ownership can be shown rather
    #: than inferred. It is the whole reason this adapter has a probe at all.
    proves_ownership = True

    def __init__(
        self,
        root: Path | None = None,
        pane_pid: int | None = None,
        session_exact: bool = False,
        session_provenance: str | TranscriptProvenance | None = None,
    ):
        #: Injectable so tests never touch the real ~/.codex.
        self.root = root or Path.home() / ".codex" / "sessions"
        #: The participant's launch process, when we have one to ask. Set only
        #: on the per-participant clone `open_source_for` builds, which is why
        #: that clone exists at all: this instance is otherwise shared by every
        #: codex session on the machine.
        self.pane_pid = pane_pid
        #: Whether the id this clone was opened with is itself proof — a resume
        #: token or a launch receipt — rather than an id read back off whatever
        #: file an earlier cwd guess happened to pick. It decides which of the
        #: two sharp keys is asked first; see `find_transcript`.
        provenance = normalize_provenance(session_provenance)
        self._session_exact = session_exact or provenance is TranscriptProvenance.EXACT
        #: Rollouts this clone has seen held open by its own process. Resolved
        #: paths, so a candidate reached by another spelling still matches.
        self._proved: set[Path] = set()

    def open_source(
        self,
        *,
        cwd: str | None,
        session_id: str | None = None,
        after: float | None = None,
        session_provenance: str | TranscriptProvenance | None = None,
        known_location: str | None = None,
    ) -> Source:
        """A source that can report a process-proven location as exact.

        A clone when the caller's provenance disagrees with this instance's.
        The two are one fact seen from two sides — which key discovery asks
        first, and how the answer is labelled — and a caller that says its id
        is exact while the observer still thinks otherwise would get the
        process asked ahead of an id it told us to trust.
        """
        provenance = normalize_provenance(session_provenance)
        session_exact = provenance is TranscriptProvenance.EXACT
        reader = self
        if session_exact != self._session_exact:
            reader = CodexObserver(
                root=self.root, pane_pid=self.pane_pid, session_exact=session_exact
            )
        return _CodexSource(
            reader,
            cwd=cwd,
            session_id=session_id,
            after=after,
            session_provenance=provenance,
            known_location=known_location,
        )

    def open_source_for(
        self,
        *,
        participant_id: str,
        cwd: str | None,
        session_id: str | None = None,
        after: float | None = None,
        session_provenance: str | TranscriptProvenance | None = None,
        known_location: str | None = None,
        pane_pid: int | None = None,
    ) -> Source:
        """Give this participant's watcher its own reader, holding its own pid.

        A clone rather than `self`, for the same reason vibe clones: the
        observer on the harness is shared by every codex session, and the pid
        — and what it has proved — is the one thing that is per-participant.
        """
        provenance = normalize_provenance(session_provenance)
        session_exact = provenance is TranscriptProvenance.EXACT
        reader = CodexObserver(root=self.root, pane_pid=pane_pid, session_exact=session_exact)
        return reader.open_source(
            cwd=cwd,
            session_id=session_id,
            after=after,
            session_provenance=provenance,
            known_location=known_location,
        )

    def proved(self, path: Path) -> bool:
        """Whether this clone's own process was found holding *path* open."""
        return _resolve(path) in self._proved

    def find_transcript(
        self,
        *,
        cwd: str,
        session_id: str | None = None,
        after: float | None = None,
    ) -> Path | None:
        if not self.root.is_dir():
            return None
        if session_id and self._session_exact:
            # An id that is itself proof outranks the process. It names the
            # file directly, so it cannot be confused by a second codex in the
            # pane, and it costs one glob rather than three subprocesses.
            hit = self._by_session_id(session_id)
            if hit is not None:
                return hit
        held = self.proven_transcript(cwd=cwd)
        if held is not None:
            return held
        if session_id:
            # Only an id we are unsure of reaches here — one read back off a
            # file some earlier cwd guess picked. Behind the process for that
            # reason: taking it first would re-derive that same wrong file
            # forever, and no later proof could ever displace it.
            hit = self._by_session_id(session_id)
            if hit is not None:
                return hit
        return self._scan_by_cwd(cwd, after)

    def _by_session_id(self, session_id: str) -> Path | None:
        """The rollout whose filename carries *session_id*.

        The uuid suffix of the filename is the session id, so this is an exact
        lookup: no scan, and no need to guess the date directory.
        """
        return next(self.root.glob(f"*/*/*/rollout-*-{session_id}.jsonl"), None)

    def proven_transcript(self, *, cwd: str | None) -> Path | None:
        """The rollout this participant's own process is holding open, if any.

        Discovery's proof half, callable on its own. A source that already has
        an admitted location needs to ask for proof *without* asking for a
        guess: `find_transcript` would fall through to the cwd scan, and
        letting a scan replace an admitted location is the drift the whole
        collision guard exists to prevent.
        """
        held = self._process_rollout(cwd)
        if held is not None:
            self._proved.add(held)
        return held

    def _scan_by_cwd(self, cwd: str, after: float | None) -> Path | None:
        """The oldest channel: newest rollout whose `session_meta` cwd matches.

        Kept exactly as it was, and reached only once the sharper keys have
        had their turn. On its own it cannot tell two siblings apart, which is
        the whole reason the process probe above it exists.
        """
        want = str(Path(cwd).resolve()) if cwd else None
        if want is None:
            return None
        candidates = []
        for path in self.root.glob("*/*/*/rollout-*.jsonl"):
            try:
                st = path.stat()
            except OSError:
                continue
            if after is not None:
                # stat, never the filename: that timestamp is local time with
                # no offset recorded, and the caller's floor is a unix epoch.
                born = getattr(st, "st_birthtime", st.st_ctime)
                if born < after:
                    continue
            candidates.append((st.st_mtime, path))
        # Collect all matches so an ambiguity is logged, not silent: two
        # siblings in the same cwd both match, and returning the newest for
        # either participant is a mis-attribution. The observer's binding
        # check (`_accept_attachment`) is the cross-cutting guarantee that refuses the
        # second binding; this method still returns the newest match so
        # rotation (the same agent writing a new transcript) works.
        matches: list[Path] = []
        for _, path in sorted(candidates, reverse=True):
            if self._transcript_cwd(path) == want:
                matches.append(path)
        if not matches:
            return None
        if len(matches) > 1:
            logger.warning(
                "codex find_transcript: %d transcripts match cwd %s; "
                "returning the newest — the observer will refuse a collision",
                len(matches),
                cwd,
            )
        return matches[0]

    def transcript_candidates(
        self,
        *,
        cwd: str | None,
        after: float | None = None,
    ) -> list[TranscriptCandidate]:
        if not self.root.is_dir():
            return []
        want = str(Path(cwd).resolve()) if cwd else None
        domain = str(self.root.resolve())
        rows = [
            self._candidate_row(path, want=want, after=after, domain=domain)
            for path in self.root.glob("*/*/*/rollout-*.jsonl")
        ]
        return sorted(rows, key=lambda c: (c.mtime or 0, c.location), reverse=True)

    def admit_operator_candidate(
        self,
        *,
        cwd: str | None,
        candidate: str,
        domain: str | None = None,
        after: float | None = None,
    ) -> TranscriptCandidate:
        want = str(Path(cwd).resolve()) if cwd else None
        root = Path(domain).resolve() if domain else self.root.resolve()
        path = Path(candidate).expanduser()
        if path.is_symlink():
            raise ValueError("candidate path is a symlink")
        real = path.resolve()
        if not real.is_relative_to(root):
            raise ValueError("candidate path is outside this harness transcript domain")
        row = self._candidate_row(real, want=want, after=after, domain=str(root))
        if row.rejection_reason:
            raise ValueError(row.rejection_reason)
        return row

    def _candidate_row(
        self,
        path: Path,
        *,
        want: str | None,
        after: float | None,
        domain: str,
    ) -> TranscriptCandidate:
        reason = None
        session_id = self.session_id(path)
        try:
            st = path.stat()
        except OSError:
            return TranscriptCandidate(
                location=str(path), rejection_reason="not readable", domain=domain
            )
        if after is not None and getattr(st, "st_birthtime", st.st_ctime) < after:
            reason = "created before participant floor"
        elif not self._is_rollout_shape(path):
            reason = "harness shape mismatch"
        elif session_id is None:
            reason = "unextractable session id"
        else:
            found_cwd = self._transcript_cwd(path)
            if found_cwd is None:
                reason = "harness mismatch or unextractable cwd"
            elif want is not None and found_cwd != want:
                reason = "cwd mismatch"
        return TranscriptCandidate(
            location=str(path),
            session_id=session_id,
            mtime=st.st_mtime,
            size=st.st_size,
            rejection_reason=reason,
            domain=domain,
        )

    def _is_rollout_shape(self, path: Path) -> bool:
        if path.suffix != ".jsonl" or _STEM.match(path.stem) is None:
            return False
        try:
            relative = path.resolve().relative_to(self.root.resolve())
        except (OSError, ValueError):
            return False
        return len(relative.parts) == 4

    def _process_rollout(self, cwd: str | None) -> Path | None:
        """The rollout this participant's own codex process holds open.

        Four conditions, all required, and the last is the one that matters:
        the file is under the configured transcript root, it is named like a
        rollout, its `session_meta` records the participant's working
        directory, and **the one process that speaks for this participant**
        holds exactly one such file open. Two would mean we do not understand
        what we are looking at, and guessing between them is the
        mis-attribution this whole path exists to prevent — so that answers
        `None` and lets the cwd scan and the reducer's guard handle it as
        before.

        Note that the process is chosen before its files are read, rather than
        pooling the open files of every codex in the pane. Pooling makes the
        count of *rollouts* stand in for the count of *possible owners*, and
        the two differ exactly when it is dangerous: two codex processes where
        only one has written its rollout yet pool to a single file, which then
        looks like proof and can be the other one's.

        The birth-time floor is deliberately not applied. It is a proxy for
        ownership, and we are holding the thing it was a proxy for; a resumed
        session whose rollout predates the participant is still that
        participant's rollout.
        """
        pid = self._owning_process()
        if pid is None:
            return None
        want = _resolve(Path(cwd)) if cwd else None
        root = _resolve(self.root)
        found: set[Path] = set()
        for path in proc.open_files(pid):
            if not self._is_rollout(path, root):
                continue
            if want is not None and self._transcript_cwd(path) != str(want):
                continue
            found.add(_resolve(path))
        if not found:
            return None
        if len(found) > 1:
            logger.warning(
                "codex process %s holds %d rollouts open under %s; "
                "declining to pick one — falling back to cwd discovery",
                pid,
                len(found),
                self.root,
            )
            return None
        return found.pop()

    def _owning_process(self) -> int | None:
        """The pane's own process, and only if that process is codex itself.

        A pane Theater spawned runs codex as the pane process, so `pane_pid`
        *is* the CLI. That identity is durable: the registry recorded the pid
        of the process it started, and while the participant lives that pid
        names that session. Anything codex spawned below it — the agent's
        tooling, or a codex it launched as a sub-agent — belongs to a
        different session, so descendants are not consulted at all.

        A pane whose root is something else, a shell for an adopted session,
        gets no answer here. Searching beneath it is what the obvious version
        of this does, and it is wrong in a way counting cannot fix: a shell
        outlives the CLI it ran. Find exactly one codex under an adopted pane
        and you have learned that one codex is running there *now*, not that
        it is the one the participant was adopted from — the operator can have
        quit the first and started a second, and the second's rollout would
        then be proved as the first's. Uniqueness is not identity.

        Closing that properly needs a durable association between the
        participant and the process, established when the pane was adopted and
        owned by the daemon, which is the only thing that may hold such state.
        Until then this fails closed: no proof for adopted panes, which leaves
        them exactly where they were — the cwd scan, and a collision guard that
        refuses what it cannot tell apart.
        """
        if self.pane_pid is None:
            return None
        if not _is_codex(proc.comm(self.pane_pid)):
            return None
        return self.pane_pid

    def _is_rollout(self, path: Path, root: Path) -> bool:
        """Under the configured root, and named the way codex names a rollout."""
        if path.suffix != ".jsonl" or _STEM.match(path.stem) is None:
            return False
        return _resolve(path).is_relative_to(root)

    def _transcript_cwd(self, path: Path) -> str | None:
        try:
            with path.open(encoding="utf-8", errors="replace") as fh:
                line = fh.readline(_CWD_PROBE_BYTES)
        except OSError:
            return None
        try:
            record = json.loads(line)
        except ValueError:
            return None
        if not isinstance(record, dict) or record.get("type") != "session_meta":
            return None
        payload = record.get("payload")
        found = payload.get("cwd") if isinstance(payload, dict) else None
        return str(Path(found).resolve()) if found else None

    def session_id(self, transcript: Path) -> str | None:
        """The uuid tail of the filename. Verified against session_meta."""
        found = _STEM.match(transcript.stem)
        return found.group(1) if found else None

    def parse(self, line: str, index: int, *, clip_text: bool = True) -> list[Event]:
        line = line.strip()
        if not line:
            return []
        try:
            record = json.loads(line)
        except ValueError:
            return []
        if not isinstance(record, dict):
            return []
        payload = record.get("payload")
        if not isinstance(payload, dict):
            return []

        ts = _epoch(record.get("timestamp"))
        kind = record.get("type")
        if kind == "event_msg":
            return self._event(payload, ts, index, clip_text=clip_text)
        if kind == "response_item":
            return self._item(payload, ts, index, clip_text=clip_text)
        # session_meta, turn_context, world_state: bookkeeping.
        return []

    def _event(
        self, payload: dict, ts: float | None, index: int, *, clip_text: bool
    ) -> list[Event]:
        _clip = clipper(clip_text)
        ptype = payload.get("type")

        if ptype == "user_message":
            raw = payload.get("message") if isinstance(payload.get("message"), str) else ""
            return [
                Event(
                    kind=EventKind.USER,
                    text=_clip(raw),
                    raw_text=raw,
                    ts=ts,
                    raw_index=index,
                )
            ]
        if ptype == "agent_message":
            if payload.get("phase") == "final_answer":
                # Repeated verbatim by the task_complete that follows it.
                # Emitting both would double every reply on the bus.
                return []
            raw = payload.get("message") if isinstance(payload.get("message"), str) else ""
            return [
                Event(
                    kind=EventKind.ASSISTANT,
                    text=_clip(raw),
                    raw_text=raw,
                    ts=ts,
                    raw_index=index,
                )
            ]
        if ptype == "task_complete":
            raw = (
                payload.get("last_agent_message")
                if isinstance(payload.get("last_agent_message"), str)
                else ""
            )
            return [
                Event(
                    kind=EventKind.ASSISTANT,
                    text=_clip(raw),
                    raw_text=raw,
                    ts=ts,
                    turn_end=True,
                    turn_id=_turn_id(payload),
                    raw_index=index,
                )
            ]
        if ptype == "turn_aborted":
            raw = f"turn aborted: {payload.get('reason') or 'unknown'}"
            return [
                Event(
                    kind=EventKind.ERROR,
                    text=raw,
                    raw_text=raw,
                    ts=ts,
                    turn_end=True,
                    turn_id=_turn_id(payload),
                    raw_index=index,
                )
            ]
        if ptype in ("mcp_tool_call_begin", "mcp_tool_call_end"):
            # The only visibility into MCP use, Theater's own tools included:
            # these calls never appear as response_items.
            invocation = payload.get("invocation")
            invocation = invocation if isinstance(invocation, dict) else {}
            tool_name = ".".join(
                str(part) for part in (invocation.get("server"), invocation.get("tool")) if part
            )
            if ptype == "mcp_tool_call_begin":
                return [
                    Event(
                        kind=EventKind.TOOL_CALL,
                        tool_name=tool_name or None,
                        ts=ts,
                        raw_index=index,
                    )
                ]
            raw = self._mcp_result(payload.get("result"))
            return [
                Event(
                    kind=EventKind.TOOL_RESULT,
                    text=_clip(raw),
                    raw_text=raw,
                    tool_name=tool_name or None,
                    ts=ts,
                    raw_index=index,
                )
            ]
        # token_count, task_started, patch_apply_end, thread_settings_applied.
        return []

    def _mcp_result(self, result) -> str:
        """Unwrap the Rust-style `{"Ok"|"Err": …}` an MCP call comes back as."""
        if not isinstance(result, dict):
            return "" if result is None else json.dumps(result, default=str)
        ok = result.get("Ok")
        if isinstance(ok, dict):
            return _flatten(ok.get("content"))
        err = result.get("Err")
        if err is not None:
            return err if isinstance(err, str) else json.dumps(err, default=str)
        return json.dumps(result, default=str)

    def _item(self, payload: dict, ts: float | None, index: int, *, clip_text: bool) -> list[Event]:
        _clip = clipper(clip_text)
        ptype = payload.get("type")

        if ptype in ("custom_tool_call", "function_call"):
            name = payload.get("name")
            paths: tuple[EventPath, ...] = ()
            if name == "apply_patch":
                # The patch markers are a structured grammar (parser.rs:39-41),
                # not prose or a shell command — extracting paths from them is
                # reading a structured field. Other custom tools (exec, wait)
                # take freeform strings whose contents are code — those yield nothing.
                raw_input = payload.get("input")
                paths = _apply_patch_paths(raw_input if isinstance(raw_input, str) else "")
            return [
                Event(
                    kind=EventKind.TOOL_CALL,
                    tool_name=name,
                    ts=ts,
                    raw_index=index,
                    paths=paths,
                )
            ]
        if ptype in ("custom_tool_call_output", "function_call_output"):
            # No tool name: the record carries only `call_id`, and resolving it
            # would mean holding state across lines, which parse() does not do.
            raw = _flatten(payload.get("output"))
            return [
                Event(
                    kind=EventKind.TOOL_RESULT,
                    text=_clip(raw),
                    raw_text=raw,
                    ts=ts,
                    raw_index=index,
                )
            ]
        # `message` duplicates the event_msg stream and `reasoning` is the
        # agent's private thinking; both are dropped, as in the Claude adapter.
        return []

    def native_children(self, transcript: Path) -> list[NativeChild]:
        """Codex has no sub-agent mechanism of its own."""
        return []

    def is_idle_screen(self, capture: str) -> bool:
        """Codex keeps a status footer below the composer.

        So the bottom line is never the prompt and `last_screen_line` — which
        both other adapters use — would never match. Instead: a running turn
        always renders `esc to interrupt`, and an idle one renders a composer
        line starting with `›` somewhere in the last few lines.

        The composer shows greyed-out placeholder text when empty ("Explain
        this codebase"), and a colourless capture cannot tell that apart from
        a human's half-typed message. That is tolerable because this method
        only feeds the AWAITING_INPUT display hint; whether a human is present
        is decided separately, from `pane_in_mode`, and never from a scrape.

        The first-launch trust dialog also trips this boolean, because it
        renders a `›` selection row just like the idle composer. That is why
        `screen_reading` must check the TRUST and APPROVAL markers before
        falling through to this method: without that guard both modals would
        classify as PROMPT and the send gate would inject into them.
        """
        if WORKING_MARKER in capture:
            return False
        lines = [line.strip() for line in capture.splitlines() if line.strip()]
        return any(line.startswith(PROMPT) for line in lines[-_SCREEN_TAIL_LINES:])

    def screen_reading(self, capture: str) -> ScreenReading:
        """Classify the rendered screen as trust, approval, working, or prompt.

        Arm order is load-bearing: both the trust dialog and the approval
        overlay render a selection row starting with `›`, so
        `is_idle_screen` returns True on both. The modal arms must therefore
        come before the `is_idle_screen` call, or both modals would classify
        as PROMPT and the send gate would inject into a live approval.
        """
        if TRUST_MARKER in capture:
            return ScreenReading(kind=ScreenKind.TRUST, confidence=ScreenConfidence.HIGH)
        if _in_screen_tail(capture, APPROVAL_MARKER):
            return ScreenReading(kind=ScreenKind.APPROVAL, confidence=ScreenConfidence.HIGH)
        if WORKING_MARKER in capture:
            return ScreenReading(kind=ScreenKind.WORKING, confidence=ScreenConfidence.HIGH)
        if self.is_idle_screen(capture):
            return ScreenReading(kind=ScreenKind.PROMPT, confidence=ScreenConfidence.HIGH)
        return ScreenReading(kind=ScreenKind.UNKNOWN, confidence=ScreenConfidence.LOW)


#: What the loader looks for. An instance, not the class (see
#: docs/harness-plugins.md). Shipped adapters meet the same contract as
#: anything in $THEATER_HOME/harnesses.
HARNESS = CodexHarness()
