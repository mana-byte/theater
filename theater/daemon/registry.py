"""Who exists, what tier they are, and how they relate to each other.

The registry is the only thing allowed to decide a participant's tier. That
decision is made once, at the moment of first contact, from evidence ranked by
how much we trust it:

    daemon spawned it        -> Spawned    identity by construction, no inference
    caller knows its pane    -> Adopted    trusted, but self-reported
    caller knows nothing     -> External   emit-only, can never be addressed

The gap between Adopted and External is not a permission setting. It is a
physical fact: inbound delivery needs a pane to type into, and External has none.
"""

from __future__ import annotations

import builtins

from theater import names
from theater.daemon import lineage
from theater.daemon.store import Store
from theater.harness import normalize
from theater.models import (
    BadRequest,
    NameTaken,
    NotFound,
    Participant,
    Status,
    Tier,
    new_id,
    now,
)


class Registry:
    def __init__(self, store: Store):
        self.store = store
        # participant id -> runtime name.  Never persisted: a daemon restart
        # regenerates every name from scratch.  Entries are never recycled —
        # a dead participant keeps its name for the daemon's lifetime, so a
        # name that appears in a user's scrollback can never later point at a
        # different agent.
        self._names: dict[str, str] = {}

    # ---- naming --------------------------------------------------------

    def _named(self, p: Participant) -> Participant:
        """Ensure *p* has a runtime name, assigning one lazily if needed.

        Lazy assignment means a daemon that restarts while agents are alive
        still names every participant on first read, rather than leaving
        pre-existing participants nameless.
        """
        if p.id not in self._names:
            self._names[p.id] = names.pick(self._names.values())
        p.name = self._names[p.id]
        return p

    # ---- reads ---------------------------------------------------------

    def get(self, pid: str) -> Participant:
        p = self.store.get_participant(pid)
        if p is None:
            raise NotFound(f"no participant {pid!r}")
        return self._named(p)

    # `builtins.` because this class defines a method named `list`, which
    # shadows the builtin for every annotation written after it.
    def list(self, *, include_dead: bool = False) -> builtins.list[Participant]:
        return [self._named(p) for p in self.store.list_participants(include_dead=include_dead)]

    def tree(self) -> builtins.list[dict]:
        """Participants as a forest, each node carrying its children inline."""
        people = self.list()
        nodes = {p.id: {**p.to_dict(), "children": []} for p in people}
        roots: list[dict] = []
        for p in people:
            node = nodes[p.id]
            parent = nodes.get(p.parent_id) if p.parent_id else None
            if parent is None:
                roots.append(node)
            else:
                parent["children"].append(node)
        return roots

    def depth_of(self, pid: str) -> int:
        """Distance from the root of the lineage. Roots are 0."""
        return lineage.depth_of(self.store, pid)

    def root_of(self, pid: str) -> str:
        """The top of this participant's lineage.

        Unlike the bare walk, this insists the participant exists: a caller
        asking the registry for a tree root wants a root, and silently getting
        back the id it passed in reads as success.
        """
        if self.store.get_participant(pid) is None:
            raise NotFound(f"no participant {pid!r}")
        return lineage.root_of(self.store, pid)

    # ---- writes --------------------------------------------------------

    def create_spawned(
        self,
        *,
        harness: str,
        cwd: str,
        parent_id: str | None = None,
        pid: str | None = None,
    ) -> Participant:
        """Reserve an id before the pane exists.

        Order matters: the id has to be minted first because it is baked into
        the MCP server argv that the pane will be launched with. The pane id is
        filled in by `attach_pane` once tmux reports it.
        """
        p = Participant(
            id=pid or new_id(),
            harness=harness,
            tier=Tier.SPAWNED,
            cwd=cwd,
            parent_id=parent_id,
            status=Status.IDLE,
        )
        self.store.upsert_participant(p)
        self.store.bus_append(
            "participant.created",
            to_id=p.id,
            from_id=parent_id,
            payload={"tier": str(p.tier), "harness": harness, "cwd": cwd},
        )
        return self._named(p)

    def attach_pane(
        self, pid: str, pane: str, *, pane_pid: int | None = None
    ) -> Participant:
        """Record where a participant lives, and which process was there.

        `pane_pid` is the launch epoch: tmux's `#{pane_pid}`, the process the
        pane was forked with. Delivery compares it against the pane's current
        pid to notice a seat that changed hands without changing its id — a
        respawn keeps the pane id and replaces everything behind it. Optional
        because a caller that could not read it (the pane vanished between
        creation and lookup) should still record the pane; a missing epoch
        turns that one check off rather than blocking delivery on it.
        """
        p = self.get(pid)
        moved = p.tmux_pane != pane
        p.tmux_pane = pane
        if pane_pid is not None:
            p.pid = pane_pid
        p.last_activity = now()
        self.store.upsert_participant(p)
        if moved:
            self.store.bus_append("participant.pane", to_id=pid, payload={"pane": pane})
        return self._named(p)

    def _evict_pane_holder(self, pane: str, *, keep: str) -> None:
        """Ensure `pane` has one holder.

        Two records pointing at one pane means a delivery could reach the wrong
        agent, so a pane is never shared. tmux does not recycle pane ids, so a
        second claimant means the seat genuinely changed hands: the user quit
        one agent and started another in the same pane. The previous occupant
        has lost its only address, and something else now answers there, so it
        is gone — not merely unreachable.
        """
        prior = self.store.find_by_pane(pane)
        if prior is None or prior.id == keep:
            return
        prior.tmux_pane = None
        self.store.upsert_participant(prior)
        self.mark_dead(prior.id)

    def _claim_pane(self, p: Participant, pane: str) -> None:
        """Record a self-reported pane, promoting External to Adopted.

        Only ever called with a pane in hand. A *missing* pane is not evidence
        of anything: `whoami` reports $TMUX_PANE, which the MCP environment
        allowlist hides, so every routine call would otherwise demote an adopted
        agent straight back to External.
        """
        if p.tmux_pane == pane:
            return
        if p.tier is Tier.SPAWNED and p.tmux_pane:
            # We read this pane id from tmux when we made the window. That
            # beats anything the occupant tells us about itself.
            return
        self._evict_pane_holder(pane, keep=p.id)
        p.tmux_pane = pane
        if p.tier is Tier.EXTERNAL:
            p.tier = Tier.ADOPTED

    def register(
        self,
        *,
        harness: str,
        pane: str | None,
        cwd: str | None,
        session_id: str | None = None,
        claimed_id: str | None = None,
    ) -> Participant:
        """First contact from an MCP server.

        Three paths converge here:
          - a spawned agent presenting the id we gave it (claimed_id hits)
          - a hand-started agent that knows its pane  -> Adopted
          - anything else                             -> External

        Also the way an External becomes addressable later: an agent that finds
        its own $TMUX_PANE and calls back with it lands in the claimed_id branch
        and is promoted. That is the whole adoption fallback, and it is the
        primary adoption path, because the MCP SDK's environment allowlist means
        a server process cannot see $TMUX_PANE for itself.
        """
        harness = normalize(harness)
        if claimed_id:
            existing = self.store.get_participant(claimed_id)
            if existing is not None:
                existing.session_id = session_id or existing.session_id
                existing.cwd = cwd or existing.cwd
                if pane:
                    self._claim_pane(existing, pane)
                existing.last_activity = now()
                self.store.upsert_participant(existing)
                self.store.bus_append("participant.hello", to_id=existing.id)
                return self._named(existing)
            # A stale id from a previous daemon lifetime. Fall through and
            # re-register rather than refusing: the agent is real either way.

        if pane:
            prior = self.store.find_by_pane(pane)
            if prior is not None:
                prior.harness = harness or prior.harness
                prior.cwd = cwd or prior.cwd
                prior.session_id = session_id or prior.session_id
                prior.status = Status.IDLE
                prior.last_activity = now()
                self.store.upsert_participant(prior)
                self.store.bus_append("participant.hello", to_id=prior.id)
                return self._named(prior)

        p = Participant(
            id=claimed_id or new_id(),
            harness=harness,
            tier=Tier.ADOPTED if pane else Tier.EXTERNAL,
            tmux_pane=pane,
            cwd=cwd,
            session_id=session_id,
            status=Status.IDLE,
        )
        self.store.upsert_participant(p)
        self.store.bus_append(
            "participant.created",
            to_id=p.id,
            payload={"tier": str(p.tier), "harness": harness, "cwd": cwd},
        )
        return self._named(p)

    # ---- naming control ------------------------------------------------

    def rename(self, pid: str, new_name: str) -> Participant:
        """Assign or change a participant's runtime name.

        *pid* may be either a participant id or the participant's current
        name, so a caller can rename by either.  The name is validated for
        format and uniqueness; renaming to the name the participant already
        holds is a no-op success, not an error.
        """
        p = self.resolve(pid)

        if new_name == self._names.get(p.id, ""):
            return p

        if not names.is_valid_name(new_name):
            raise BadRequest(
                f"invalid name {new_name!r}: must match ^[A-Za-z][A-Za-z0-9_-]"
                f"{{0,23}}$ (e.g. Arlequin, Scapin-2)"
            )

        # A 12-char pure-hex name would be indistinguishable from a
        # participant id and make resolve ambiguous.
        if len(new_name) == 12 and all(c in "0123456789abcdef" for c in new_name.casefold()):
            raise BadRequest(
                f"name {new_name!r} looks like a participant id; "
                f"choose a name that cannot be confused with one"
            )

        for other_id, other_name in self._names.items():
            if other_id != p.id and other_name.casefold() == new_name.casefold():
                raise NameTaken(f"name {new_name!r} is taken by participant {other_id!r}")

        self._names[p.id] = new_name
        p.name = new_name
        self.store.bus_append("participant.renamed", to_id=p.id, payload={"name": new_name})
        return p

    def resolve(self, token: str) -> Participant:
        """Find a participant by id or by name (case-insensitive).

        Names only get looked up after ids miss, so a short word can never be
        confused with a 12-char id.  Materializes names for every participant
        first, because a participant nobody has read yet has no entry in the
        name map.
        """
        # Ensure every participant has a name before searching by name.
        self.list()

        p = self.store.get_participant(token)
        if p is not None:
            return self._named(p)

        for pid, name in self._names.items():
            if name.casefold() == token.casefold():
                return self._named(self.get(pid))

        raise NotFound(f"no participant {token!r}")

    def set_status(self, pid: str, status: Status) -> None:
        self.get(pid)
        self.store.set_status(pid, status)
        self.store.bus_append("participant.status", to_id=pid, payload={"status": str(status)})

    def mark_dead(self, pid: str) -> None:
        p = self.store.get_participant(pid)
        if p is None or p.status is Status.DEAD:
            return
        self.store.set_status(pid, Status.DEAD)
        self.store.bus_append("participant.dead", to_id=pid)

    def touch(self, pid: str) -> None:
        self.store.touch(pid)
