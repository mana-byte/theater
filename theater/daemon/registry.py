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
from collections.abc import Callable

from theater import names
from theater.constants.daemon import BUS_KIND_PARTICIPANT_METADATA_CHANGED
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
    normalize_participant_description,
    now,
)


class Registry:
    def __init__(self, store: Store):
        self.store = store
        # participant id -> runtime name; never persisted, only live participants.
        self._names: dict[str, str] = {}
        self._participant_cleanup: list[Callable[[str], None]] = []

    def add_participant_cleanup(self, callback: Callable[[str], None]) -> None:
        """Register bounded runtime cleanup for a dead participant."""
        self._participant_cleanup.append(callback)

    def _cleanup_participant(self, participant_id: str) -> None:
        for callback in tuple(self._participant_cleanup):
            callback(participant_id)

    # ---- naming --------------------------------------------------------

    def _named(self, p: Participant) -> Participant:
        """Ensure *p* has a runtime name, assigning one lazily if needed.

        Lazy assignment means a daemon that restarts while agents are alive
        still names every participant on first read, rather than leaving
        pre-existing participants nameless.

        DEAD participants never get a name — they are nameless on read and
        their names are released on death so the pool is not exhausted by
        corpses.  A stale mapping left behind by a store-level status change
        is self-healed here: if the row is DEAD but a name entry lingers, it
        is purged on sight.
        """
        if p.status is Status.DEAD:
            self._names.pop(p.id, None)
            p.name = None
            return p
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

    # `builtins.` because this class defines a `list` method shadowing the builtin in annotations.
    def list(
        self,
        *,
        include_dead: bool = False,
        ids: builtins.list[str] | None = None,
        parent_id: str | None = None,
        after: tuple[float, str] | None = None,
        limit: int | None = None,
    ) -> builtins.list[Participant]:
        return [
            self._named(p)
            for p in self.store.list_participants(
                include_dead=include_dead,
                ids=ids,
                parent_id=parent_id,
                after=after,
                limit=limit,
            )
        ]

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

    def live_count(self) -> int:
        """Count of participants whose status is not DEAD."""
        return self.store.live_count()

    def addressable_count(self) -> int:
        """Count of participants matching ``Participant.addressable``."""
        return self.store.addressable_count()

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
        has_prompt: bool | None = None,
        resumed_from_id: str | None = None,
        name: str | None = None,
        description: str | None = None,
    ) -> Participant:
        """Reserve an id before the pane exists.

        Order matters: the id has to be minted first because it is baked into
        the MCP server argv that the pane will be launched with. The pane id is
        filled in by `attach_pane` once tmux reports it.

        `has_prompt` says whether the spawn carried a task, which is what tells
        the régie a new child is worth animating. It defaults to None — "nobody
        said" — rather than False, because False is an answer: a future caller
        that forgot the argument would assert the spawn was promptless instead
        of admitting it did not know. The one caller that does know (`Spawner`)
        passes it explicitly.
        """
        p = Participant(
            id=pid or new_id(),
            harness=harness,
            tier=Tier.SPAWNED,
            cwd=cwd,
            parent_id=parent_id,
            resumed_from_id=resumed_from_id,
            status=Status.IDLE,
            description=(
                normalize_participant_description(description) if description is not None else None
            ),
        )
        if name is not None:
            self._validate_name(p.id, name)
            self._names[p.id] = name
            p.name = name
        try:
            self.store.upsert_participant(p)
        except BaseException:
            self._names.pop(p.id, None)
            raise
        self.store.bus_append(
            "participant.created",
            to_id=p.id,
            from_id=parent_id,
            payload={
                "tier": str(p.tier),
                "harness": harness,
                "cwd": cwd,
                "has_prompt": has_prompt,
            },
        )
        return self._named(p)

    def attach_pane(
        self,
        pid: str,
        pane: str,
        *,
        pane_pid: int | None = None,
        tmux_server_identity: str | None = None,
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
        if tmux_server_identity is not None:
            p.tmux_server_identity = tmux_server_identity
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
        agent, so a pane is never shared. Within one tmux server, a second
        claimant means the seat genuinely changed hands: the user quit one
        agent and started another in the same pane. The previous occupant has
        lost its only address, and something else now answers there, so it is
        gone — not merely unreachable.
        """
        prior = self.store.find_by_pane(pane)
        if prior is None or prior.id == keep:
            return
        prior.tmux_pane = None
        self.store.upsert_participant(prior)
        self.mark_dead(prior.id)

    def _claim_pane(self, p: Participant, pane: str, *, tmux_server_identity: str | None) -> None:
        """Record a self-reported pane, promoting External to Adopted.

        Only ever called with a pane in hand. A *missing* pane is not evidence
        of anything: `whoami` reports $TMUX_PANE, which the MCP environment
        allowlist hides, so every routine call would otherwise demote an adopted
        agent straight back to External.
        """
        if p.tmux_pane == pane:
            if tmux_server_identity is not None:
                p.tmux_server_identity = tmux_server_identity
            return
        if p.tier is Tier.SPAWNED and p.tmux_pane:
            # We read this pane id from tmux when we made the window; it beats occupant reports.
            return
        self._evict_pane_holder(pane, keep=p.id)
        p.tmux_pane = pane
        p.tmux_server_identity = tmux_server_identity
        if p.tier is Tier.EXTERNAL:
            p.tier = Tier.ADOPTED

    def register(
        self,
        *,
        harness: str,
        pane: str | None,
        pane_pid: int | None = None,
        cwd: str | None,
        session_id: str | None = None,
        claimed_id: str | None = None,
        tmux_server_identity: str | None = None,
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
                if (
                    pane
                    and tmux_server_identity is not None
                    and existing.tmux_server_identity not in (None, tmux_server_identity)
                ):
                    claimed_id = None
                else:
                    existing.session_id = session_id or existing.session_id
                    existing.cwd = cwd or existing.cwd
                    if pane:
                        self._claim_pane(
                            existing,
                            pane,
                            tmux_server_identity=tmux_server_identity,
                        )
                        if existing.tmux_pane == pane and pane_pid is not None:
                            existing.pid = pane_pid
                    # Converge an alias-stored harness on reconnect (guard: normalize() == harness).
                    if normalize(existing.harness) == harness and existing.harness != harness:
                        existing.harness = harness
                    existing.last_activity = now()
                    self.store.upsert_participant(existing)
                    self.store.bus_append("participant.hello", to_id=existing.id)
                    return self._named(existing)
            # A stale id from a previous daemon lifetime; fall through and re-register.

        if pane:
            prior = self.store.find_by_pane(pane)
            if (
                prior is not None
                and tmux_server_identity is not None
                and prior.tmux_server_identity not in (None, tmux_server_identity)
            ):
                self.mark_dead(prior.id)
                prior = None
            if prior is not None and (
                tmux_server_identity is None
                or prior.tmux_server_identity in (None, tmux_server_identity)
            ):
                prior.harness = harness or prior.harness
                prior.cwd = cwd or prior.cwd
                prior.session_id = session_id or prior.session_id
                if tmux_server_identity is not None:
                    prior.tmux_server_identity = tmux_server_identity
                if pane_pid is not None:
                    prior.pid = pane_pid
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
            tmux_server_identity=tmux_server_identity if pane else None,
            pid=pane_pid if pane else None,
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

    def _validate_name(self, participant_id: str, value: str) -> None:
        """Validate a live-only name without changing its owner."""
        if not isinstance(value, str) or not names.is_valid_name(value):
            raise BadRequest(
                f"invalid name {value!r}: must match ^[A-Za-z][A-Za-z0-9_-]"
                f"{{0,23}}$ (e.g. Arlequin, Scapin-2)"
            )
        if len(value) == 12 and all(c in "0123456789abcdef" for c in value.casefold()):
            raise BadRequest(
                f"name {value!r} looks like a participant id; "
                "choose a name that cannot be confused with one"
            )
        self.list()
        for other_id, other_name in self._names.items():
            if other_id != participant_id and other_name.casefold() == value.casefold():
                raise NameTaken(f"name {value!r} is taken by participant {other_id!r}")

    def rename(self, pid: str, new_name: str) -> Participant:
        """Assign or change a participant's runtime name.

        *pid* may be either a participant id or the participant's current
        name, so a caller can rename by either.  The name is validated for
        format and uniqueness; renaming to the name the participant already
        holds is a no-op success, not an error.

        Renaming a dead participant is refused: a dead participant has no
        runtime name (it was released on death), so there is nothing to
        change and accepting the call would re-enter an id into _names that
        mark_dead just removed.
        """
        p = self.resolve(pid)

        if p.status is Status.DEAD:
            raise BadRequest(
                f"cannot rename participant {pid!r}: it is dead; "
                f"dead participants have no runtime name to change. "
                f"Use the participant id {pid!r} for historical access."
            )

        if new_name == self._names.get(p.id, ""):
            return p

        self._validate_name(p.id, new_name)

        self._names[p.id] = new_name
        p.name = new_name
        self.store.bus_append("participant.renamed", to_id=p.id, payload={"name": new_name})
        return p

    def update_metadata(
        self,
        pid: str,
        *,
        name: str | None = None,
        description: str | None = None,
    ) -> Participant:
        """Update supplied participant metadata after validating every field."""
        if name is None and description is None:
            raise BadRequest(
                "supply at least one of name or description to update participant metadata"
            )
        p = self.resolve(pid)
        if p.status is Status.DEAD:
            raise BadRequest(f"cannot update participant {pid!r}: it is dead")

        normalized_description = (
            normalize_participant_description(description)
            if description is not None
            else p.description
        )
        if name is not None:
            self._validate_name(p.id, name)

        changed: list[str] = []
        if description is not None and normalized_description != p.description:
            p.description = normalized_description
            self.store.upsert_participant(p)
            changed.append("description")
        if name is not None and name != self._names.get(p.id):
            self._names[p.id] = name
            p.name = name
            changed.append("name")
        if changed:
            self.store.bus_append(
                BUS_KIND_PARTICIPANT_METADATA_CHANGED,
                to_id=p.id,
                payload={"fields": changed},
            )
        return self._named(p)

    def resolve(self, token: str) -> Participant:
        """Find a participant by id or by name (case-insensitive).

        Names only get looked up after ids miss, so a short word can never be
        confused with a 12-char id.  Materializes names for every live
        participant first, because a participant nobody has read yet has no
        entry in the name map.

        A dead row found by exact id is returned (with name=None) rather than
        falling through to a name search — the id is unambiguous, and a name
        that happens to match the token must never shadow a real id.  A
        stale name entry pointing at a dead or missing participant is cleaned
        on sight rather than followed.
        """
        # Ensure every live participant has a name before searching by name.
        self.list()

        # Id-first: an exact id match always wins, even for a dead row.
        p = self.store.get_participant(token)
        if p is not None:
            return self._named(p)

        # Name search: follow only live participants; stale mappings are purged on sight.
        for pid, name in list(self._names.items()):
            if name.casefold() != token.casefold():
                continue
            owner = self.store.get_participant(pid)
            if owner is None or owner.status is Status.DEAD:
                self._names.pop(pid, None)
                continue
            return self._named(owner)

        raise NotFound(f"no participant {token!r}")

    def set_status(self, pid: str, status: Status) -> None:
        if status is Status.DEAD:
            # Validate existence so set_status(missing, DEAD) raises NotFound.
            if self.store.get_participant(pid) is None:
                raise NotFound(f"no participant {pid!r}")
            self.mark_dead(pid)
            return
        self.get(pid)
        self.store.set_status(pid, status)
        self.store.bus_append("participant.status", to_id=pid, payload={"status": str(status)})

    def mark_dead(self, pid: str) -> None:
        p = self.store.get_participant(pid)
        if p is None or p.status is Status.DEAD:
            # Already dead or gone: still purge stale name entry so the mask can be reused.
            self._names.pop(pid, None)
            self.store.delete_receipt_token(pid)
            self.store.delete_channel_credentials(pid)
            self.store.delete_mcp_plugin_credentials(pid)
            self._cleanup_participant(pid)
            return
        self.store.set_status(pid, Status.DEAD)
        self.store.delete_receipt_token(pid)
        self.store.delete_channel_credentials(pid)
        self.store.delete_mcp_plugin_credentials(pid)
        self._cleanup_participant(pid)
        self._names.pop(pid, None)
        self.store.bus_append("participant.dead", to_id=pid)

    def finalize_tmux_restarted(self, participants: builtins.list[Participant]) -> None:
        for p in participants:
            self.store.delete_receipt_token(p.id)
            self.store.delete_channel_credentials(p.id)
            self.store.delete_mcp_plugin_credentials(p.id)
            self._cleanup_participant(p.id)
            self._names.pop(p.id, None)

    def touch(self, pid: str) -> None:
        self.store.touch(pid)
