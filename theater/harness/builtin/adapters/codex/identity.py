from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, cast

from theater import proc
from theater.harness.source import Source, TranscriptCandidate
from theater.provenance import TranscriptProvenance, normalize_provenance

from .constants import (
    _CWD_PROBE_BYTES,
    _LOSS_CANDIDATE_PROBES,
    _STEM,
    CODEX_SESSION_META_RECORD_TYPE,
)
from .launch import CodexHarness
from .source import _CodexSource

logger = logging.getLogger("theater.harness.codex")


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


class CodexIdentityMixin:
    if TYPE_CHECKING:
        root: Path
        pane_pid: int | None
        _proved: set[Path]
        _session_exact: bool

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
        from .observer import CodexObserver

        provenance = normalize_provenance(session_provenance)
        session_exact = provenance is TranscriptProvenance.EXACT
        reader = cast(CodexObserver, self)
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
        from .observer import CodexObserver

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
            # An id that is itself proof outranks the process; names the file, one glob.
            hit = self._by_session_id(session_id)
            if hit is not None:
                return hit
        held = self.proven_transcript(cwd=cwd)
        if held is not None:
            return held
        if session_id:
            # Only an unsure id reaches here — read from a file an earlier guess picked.
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
                # stat, not the filename: its timestamp is local, no offset; caller floor is epoch.
                born = getattr(st, "st_birthtime", st.st_ctime)
                if born < after:
                    continue
            candidates.append((st.st_mtime, path))
        # Collect all matches so an ambiguity is logged, not silent: two siblings in the same cwd.
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
        domain: str | None = None,
        after: float | None = None,
    ) -> list[TranscriptCandidate]:
        root = Path(domain).resolve() if domain else self.root.resolve()
        if not root.is_dir():
            return []
        want = str(Path(cwd).resolve()) if cwd else None
        resolved_domain = str(root)
        rows = [
            self._candidate_row(path, want=want, after=after, domain=resolved_domain)
            for path in root.glob("*/*/*/rollout-*.jsonl")
        ]
        return sorted(rows, key=lambda c: (c.mtime or 0, c.location), reverse=True)

    def identity_loss_candidate(
        self,
        *,
        cwd: str | None,
        current: Path,
        current_mtime_ns: int,
        after: float | None = None,
    ) -> Path | None:
        """A newer cwd match, bounded and never itself process proof."""
        if not self.root.is_dir() or not cwd:
            return None
        want = str(Path(cwd).resolve())
        candidates: list[tuple[int, Path]] = []
        for path in self.root.glob("*/*/*/rollout-*.jsonl"):
            if path == current or path.is_symlink():
                continue
            try:
                st = path.stat()
            except OSError:
                continue
            if st.st_mtime_ns <= current_mtime_ns:
                continue
            if after is not None and getattr(st, "st_birthtime", st.st_ctime) < after:
                continue
            candidates.append((st.st_mtime_ns, path))
        for _mtime, path in sorted(candidates, reverse=True)[:_LOSS_CANDIDATE_PROBES]:
            if self._transcript_cwd(path) == want:
                return path
        return None

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
        elif not self._is_rollout_shape(path, root=Path(domain)):
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

    def _is_rollout_shape(self, path: Path, *, root: Path | None = None) -> bool:
        if path.suffix != ".jsonl" or _STEM.match(path.stem) is None:
            return False
        try:
            relative = path.resolve().relative_to((root or self.root).resolve())
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
        if not isinstance(record, dict) or record.get("type") != CODEX_SESSION_META_RECORD_TYPE:
            return None
        payload = record.get("payload")
        found = payload.get("cwd") if isinstance(payload, dict) else None
        return str(Path(found).resolve()) if found else None

    def session_id(self, transcript: Path) -> str | None:
        """The uuid tail of the filename. Verified against session_meta."""
        found = _STEM.match(transcript.stem)
        return found.group(1) if found else None
