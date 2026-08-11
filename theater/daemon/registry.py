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

from theater.daemon.store import Store
from theater.harness import normalize
from theater.models import (
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

    # ---- reads ---------------------------------------------------------

    def get(self, pid: str) -> Participant:
        p = self.store.get_participant(pid)
        if p is None:
            raise NotFound(f"no participant {pid!r}")
        return p

    def list(self, *, include_dead: bool = False) -> list[Participant]:
        return self.store.list_participants(include_dead=include_dead)

    def tree(self) -> list[dict]:
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
        """Distance from the root of the lineage. Roots are 0.

        Walks parent links with a visited set: a cycle in the stored lineage
        would otherwise hang the daemon, and the depth cap is a safety rail, so
        it must not itself be the unsafe part.
        """
        depth = 0
        seen = {pid}
        current = self.store.get_participant(pid)
        while current is not None and current.parent_id:
            if current.parent_id in seen:
                break
            seen.add(current.parent_id)
            depth += 1
            current = self.store.get_participant(current.parent_id)
        return depth

    def root_of(self, pid: str) -> str:
        seen = {pid}
        current = self.store.get_participant(pid)
        if current is None:
            raise NotFound(f"no participant {pid!r}")
        while current.parent_id and current.parent_id not in seen:
            seen.add(current.parent_id)
            parent = self.store.get_participant(current.parent_id)
            if parent is None:
                break
            current = parent
        return current.id

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
            status=Status.STARTING,
        )
        self.store.upsert_participant(p)
        self.store.bus_append(
            "participant.created",
            to_id=p.id,
            from_id=parent_id,
            payload={"tier": str(p.tier), "harness": harness, "cwd": cwd},
        )
        return p

    def attach_pane(self, pid: str, pane: str, *, harness_pid: int | None = None) -> Participant:
        p = self.get(pid)
        p.tmux_pane = pane
        p.pid = harness_pid
        p.last_activity = now()
        self.store.upsert_participant(p)
        self.store.bus_append("participant.pane", to_id=pid, payload={"pane": pane})
        return p

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
            # We read this pane id out of tmux ourselves when we made the
            # window. That beats anything the occupant tells us about itself.
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
                existing.status = (
                    Status.IDLE if existing.status is Status.STARTING else existing.status
                )
                existing.session_id = session_id or existing.session_id
                existing.cwd = cwd or existing.cwd
                if pane:
                    self._claim_pane(existing, pane)
                existing.last_activity = now()
                self.store.upsert_participant(existing)
                self.store.bus_append("participant.hello", to_id=existing.id)
                return existing
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
                return prior

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
        return p

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
