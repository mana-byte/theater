"""The stdio MCP server every participant runs.

One process per participant lane. Its whole job is to translate tool calls into
daemon RPCs on behalf of exactly one participant, whose id arrives on argv.
Harnesses may use separate control and wait lanes:

    theater mcp --id <participant-id> [--harness <name>] [--toolset all]

argv, not the environment, because the MCP SDK strips everything outside a
six-variable allowlist when it launches a stdio server
(mcp/client/stdio/__init__.py:127). $THEATER_ID is still read as a fallback for
harnesses that do pass their environment through.
"""

from __future__ import annotations

import inspect
import os
from contextlib import asynccontextmanager
from typing import Any, Literal

from mcp.server import MCPServer
from mcp.types import CallToolResult

from theater.constants.daemon import RPC_DEFAULT_MAX_WAIT_SECONDS, RPC_MAX_AWAIT_SECONDS
from theater.constants.harness import (
    CLAUDE_MCP_AUTO_BACKGROUND_DEFAULT_MS,
    CLAUDE_MCP_AUTO_BACKGROUND_ENV,
    CLAUDE_MCP_AUTO_BACKGROUND_MARGIN_SECONDS,
    HARNESS_MCP_SERVER_NAME,
)
from theater.harness import describe
from theater.harness.registry.mcp import ErrorDelivery, mcp_error_delivery
from theater.mcp import tools
from theater.mcp.arguments import ignored_arguments, with_ignored_arguments
from theater.mcp.client_pool import DaemonClientPool
from theater.mcp.instrumentation import ToolTiming
from theater.mcp.refusals import refusal_envelope, refusal_result
from theater.mcp.result_delivery import with_structured_object
from theater.mcp.tools import Session

#: `spawn_session`'s description — harness list from local registry (hint); daemon is authoritative.
INSTRUCTIONS = """Theater makes the other agents on this machine addressable,
so you can hand work to them and collect it back.

spawn_session starts a child; send gives an agent that is already running more work.
Each returns one handle, and await_sessions waits for it and returns the answer. Theater
queues a message for a busy peer and delivers it when that is safe, so you never poll or
retry; cancel_job withdraws work that has not started.

To pass one agent's result to the next, name it: after="Writer" on spawn_session or send.
Theater waits for that work, then adds the facts (outcome, pinned commit, the answer) to the
task you wrote, so you copy no path, hash or handle. A name means the work that agent has
already started (its running job, else its latest), never a message still queued for it: if
your send returned delivery="queued", pass that send's handle instead. For a review that must
not inherit the author's conclusions, spawn_session(task_kind="independent_review",
worktree=true, after=...) gives the reviewer the facts, not the verdict, in a worktree at the
exact commit.

Ask for a structured answer with response_format when you will act on it; await_sessions
returns validated JSON in structured_result. A child's "done" is the end of its turn, not a
verdict on its work: check the repo or a review before you rely on it.
read_transcript and get_session_controls diagnose a stuck, failed or surprising child;
they are not how you read answers.

Children that share a file do not run in parallel, they produce a conflict; split the
work so each owns files no sibling touches.

Address participants by name. Every `target` accepts a live name, and names are
unique among live participants, so you never need to look up or memorize an id
to talk to a live agent. Keep ids only for dead participants and resume.

If Theater gave you a task, your final message ends that job and is the answer your
requester's await returns (about 2,000 chars unless it set response_format). Put the result
there; do not send it to your parent, which only queues a new prompt in its terminal.

Wait with await_sessions, never a shell sleep or poll loop. A refused call
returns a code, and `next` when known: follow it instead of retrying unchanged.
Only ask the user to kill a child once its work is merged or abandoned. Children in a
position your play lists as disposable need no ask.

When the user's request names a workflow (a debate, a delegation, a review, Theater setup
or recovery), call list_skills before building it from raw tools: a skill usually encodes
the safe choreography, and its description says when it applies. Then load_skill that exact
skill. To give another agent a skill, name it in skills=[...] on spawn_session or send;
Theater attaches it after your task, and a child never inherits skills it was not given.
A play is a team structure named in list_skills; loading its master skill starts it. While
its master lives, every spawn below it attaches one position the spawner may give.
"""

_TOOLSETS = frozenset({"all", "control", "wait"})
_WAIT_TOOL = "await_sessions"

AWAIT_DOC = """Wait for child completion, an input request, or a human to leave.

handles: spawn/send job handles, or registered participant ids without a job.
max_wait: one overall deadline including human holds; default 150 seconds, cap {cap}.
Keep it shorter than your MCP client's timeout. Timeout grants no permission to send.

Returns when ANY target qualifies, preserving input order and returning all entries.
Presence is gated once, at admission. A target with a human present or unknown at admission
needs an observed departure (permanent: re-entry does not restore the gate) plus a terminal
job (done, crashed, or killed) or a current awaiting_input status; departure alone never releases
a running job. A target with no human at admission is never gated: a terminal job, or a running
job whose participant is awaiting_input, qualifies it. Input requests are rechecked on every
status change. Ids with no job wait only for presence and return at once if unprotected. A
protected target can still return timeout or pending under unknown presence even when its job
is terminal.
A spawned child's id is also its spawn job's handle, and a job handle wins over the
participant-id reading: awaiting the child's id returns that first task, not later sends.
Await each send's own handle.

Each entry carries handle, state (running/done/crashed/killed), participant_status
(observed independently, possibly working after departure) and await_reason; error_code,
error (what happened and what to do) and finished_at appear once set; running entries
carry quiet_s, seconds of output silence.
human_presence (state, protected, reason) appears only while it is not a plain absence.
await_reason is one of: job_terminal, presence_released,
awaiting_input (a running job needs input; terminal state takes precedence), already_absent,
timeout, or pending (another target released wait-any). For gated terminal jobs, job_terminal
and presence_released name the condition observed last — completion or departure.
awaiting_input means the child is blocked on an approval, trust or other dialog that only a
human can answer. send cannot answer it: it is refused as awaiting_decision, or by default
queued until the dialog closes. Ask the user to answer it in the child's terminal, then
re-await; never sleep-poll it. The entry includes pending_interaction (kind, optional
native_turn_id and details) when an exact cached native snapshot supplies it; this result
grants no permission to mutate the child. No-job entries omit state.
Re-await a pending or timed-out entry only while its job or presence can still change.
max_wait=0 returns current state without blocking (running entries read await_reason=timeout):
use it for status sweeps.

A spawn or send given `after` shows `handoff` (state, source, ...) and stays running while it
waits for its source; if it never launched (cancelled, refused), its terminal state alone
releases the await.

Done means the turn ended, not that its work is correct. `result` is a bounded preview of the
child's final message (about 2,000 chars): its claim, not evidence; check the repo.
With response_format, a spawned or sent job returns its final answer as a native JSON value
in structured_result instead of result. structured_status is "parsed"; "unavailable" when completion
failed, output was missing, or a hint-only answer was not JSON; or "unable_to_parse" when a strict
contract never produced a valid answer (the job is done; its result holds the last answer read).
A finished job also carries changes: either
{"evidence":"none"}, or {"evidence":"tool_paths","modified":[first 20 paths],
"modified_count":int,"read_count":int}; evidence "none" means Theater saw no file paths, not
that nothing changed. Process every qualified entry; several may be ready together.
"""


def await_ceiling(harness: str) -> float | None:
    """Longest wait this client keeps in the foreground; None when it never backgrounds."""
    if harness != "claude":
        return None
    raw = os.environ.get(CLAUDE_MCP_AUTO_BACKGROUND_ENV)
    try:
        ms = float(raw) if raw else CLAUDE_MCP_AUTO_BACKGROUND_DEFAULT_MS
    except ValueError:
        ms = CLAUDE_MCP_AUTO_BACKGROUND_DEFAULT_MS
    if ms <= 0:
        return None
    return max(ms / 1000 - CLAUDE_MCP_AUTO_BACKGROUND_MARGIN_SECONDS, 0.0)


def _await_doc(ceiling: float | None) -> str:
    cap = RPC_MAX_AWAIT_SECONDS if ceiling is None else min(ceiling, RPC_MAX_AWAIT_SECONDS)
    return AWAIT_DOC.replace("{cap}", f"{cap:g}")


def _instructions(toolset: str) -> str:
    """Return guidance appropriate to the server's deliberately small surface."""
    if toolset == "control":
        return (
            f"{INSTRUCTIONS}\n\n"
            "await_sessions lives on the separate theater_wait MCP server, so a "
            "cancelled long wait never delays control calls such as interrupt_session."
        )
    if toolset == "wait":
        return (
            "This is Theater's wait-only MCP server. It exposes only "
            "await_sessions; use the theater MCP server for every other Theater operation."
        )
    return INSTRUCTIONS


def _includes_tool(toolset: str, name: str) -> bool:
    """Select the toolset's exposed tools."""
    if toolset == "all":
        return True
    if toolset == "control":
        return name != _WAIT_TOOL
    return name == _WAIT_TOOL


SPAWN_DOC = """Start a new child agent through Theater's selected terminal provider.
For a debate, delegation, review or other named workflow, check list_skills first: a skill
usually encodes the safe way to spawn it.
If you will act on the answer, pass response_format ({{"$theater": "review"}}, "delegate" or
"debate"): await_sessions then returns validated JSON. Without it you get only a ~2,000-char
free-text preview of the child's final message.

harness:  which CLI to run. This machine has: {harnesses}. Call
          list_harnesses for the daemon's own answer, which is the one that
          decides whether a spawn succeeds.
prompt:   the task, delivered on the child's command line at startup.
          Optional; omit it or pass null to start a plain CLI with no task.
          The child inherits nothing you know, so state the goal, the files it
          may change, what it must leave alone, how to check its own work, and
          what to report back. With `after`, Theater adds the source's context;
          you still write the task.
after:    a live participant name or an exact job handle. A name means the work
          that agent has already started (its running job, else its latest),
          never a message still queued for it: if your send returned
          delivery="queued", pass that send's handle. The child is reserved
          now and launches, with the task you wrote plus a labelled card about
          that work, only when it ends: no worktree, argv or terminal exists
          before. The same handle follows the whole life: await_sessions waits
          for the dependency and then for the child, and cancel_job withdraws it
          while it waits. The card has the source's outcome and its pinned Git
          commit and dirty state, and, for a task, its answer marked unverified;
          a failed source gives a labelled failure. Requires a prompt; not with
          resume. Awaiting it from the source's own work is a cycle, refused.
task_kind: "work" (default) or "independent_review", needing `after`.
          "independent_review" gives the reviewer the source's facts without its
          conclusions and requires worktree=true: the isolated worktree is based
          on exactly the source's pinned commit, and the review is refused
          (source_not_reviewable) if the source left uncommitted changes or no
          pinned commit. Omit base_branch with it.
response_format: optional JSON object. A bare JSON Schema object is a prompt hint
          only: the daemon injects it once and parses the whole final answer with
          json.loads, with no validation, scraping, or retry. A tagged object is a strict
          contract. Presets, each with required fields: {{"$theater": "review"}} (verdict,
          reviewed, summary, findings), {{"$theater": "debate"}} (verdict, position,
          strongest_evidence, weakest_assumption, position_change, remaining_blocker),
          {{"$theater": "delegate"}} (status, summary, blockers). They take optional fields
          (commit, changed_paths, checks, findings, ...) and extra keys, are written for a
          caller that never reads the transcript, and are capped at 16,384 characters.
          Or {{"$theater": "custom", "name": "x", "schema": {{...}}}}: a name is registered to
          your participant session (the same schema is idempotent, a different one is
          refused); omit schema to reuse it. Without a session a schema works inline only.
          Custom schemas are JSON Schema 2020-12 limited to type, enum, const, properties,
          required, additionalProperties, items, prefixItems, and size/range bounds, at most
          32 KiB and 24 levels; no $ref, anyOf/oneOf/allOf/not, if/then/else, or regexes.
          Answers hold at most 65,536 characters and 5,000 values. Theater validates the
          whole final answer against the schema. Where it can prove which turn answered, it
          asks the same agent to correct an invalid answer a bounded number of times; the
          job stays running and you see nothing of it.
          Otherwise, or when that fails, the job ends with structured_status
          "unable_to_parse". await_sessions returns a valid answer in structured_result.
skills:   optional list of skill names from list_skills. Theater appends each one's
          instructions to your task as a labelled card, after any handoff card and
          before response_format guidance, so the child needs no load_skill call.
          Omitted or empty means none: a child never inherits your skills. Needs a
          prompt; at most 16 KiB combined; refused for an unknown or disabled skill
          and with resume where the harness cannot carry a prompt. A play's master
          skill makes the child the master of a new play; other play skills need a
          live play. Inside one, attach exactly one position your own position may
          give (list_skills shows them), at most one task role that fits it, and any
          standalone skills; a resumed member keeps its position, resumed by an agent
          that may give it. The refusal lists what you may attach.
approval: "manual" | "edits" | "yolo" — required, no default. This is
          the only thing standing between an unattended child and your
          filesystem, so choose it deliberately. Unattended, only "yolo"
          runs without stopping: "manual" and "edits" halt at every
          permission prompt they do not auto-approve, and only a human can
          answer it (await_sessions then shows awaiting_input). Use them
          when a human watches the child's terminal. For a read-only task
          (review, research, debate) the built-in skills give "yolo" a
          read-only brief. list_harnesses shows which modes each harness
          offers; a null `approvals` means the daemon predates the field —
          restart it rather than guess.
cwd:      where the child works. Defaults to your own directory.
          worktree=true requires cwd inside a git repo.
provider: exact provider id or selector for this launch. When omitted, Theater
          uses terminals.default_provider. A stock-terminal launch needs that
          provider ready; for tmux, have the operator run `regie bridge start`.
model:    which model the child runs, spelled the way its own CLI spells it
          (opencode wants provider/model). Omitting it bypasses the model
          allowlist and leaves selection to the CLI; it does not guarantee
          launch success. Naming one only works if the config allows it for
          that harness — call list_harnesses(include_models=true) for the set,
          which is usually empty until a human writes it. Theater checks
          membership and nothing else: it cannot confirm the CLI honoured the
          name, so a typo someone allowed surfaces as a child on the wrong
          model, not as an error here.
reasoning_effort: reasoning/thinking effort for the child (e.g. "low",
          "medium", "high"). Optional; omit it and the harness uses its
          default. Mirrors `model`: the config must list the effort for that
          harness under [reasoning], and the adapter must accept it. Not all
          harnesses support it — list_harnesses(include_models=true) shows
          `reasoning_supported`.
          Theater checks membership and nothing else.
worktree: true creates an isolated worktree and branch for the child (the branch
          is in the result; merge it explicitly). What is isolated is the index,
          not the merge: give siblings files no sibling touches. A non-empty
          string creates or joins a named shared worktree (branch
          theater/named/<name>): live children with that name share one
          directory, index and HEAD, so concurrent git add/commit can
          interfere and file claims are not enforced. base_branch applies only
          at creation; a join omits it or repeats it exactly. A named worktree
          survives teardown until `theater workspaces cleanup <workspace_id>
          --delete-branch`. Needs cwd inside a git repo; not with resume.
name:      optional alias: 1-24 chars, letter first, then letters/digits/_/-
           (e.g. Scapin-2); omit it to get a free one. Unique while the child
           lives (case-insensitive), released at death, not inherited on resume.
description: highly recommended for user clarity: a short, plain, specific,
           non-verbose summary of the child's purpose (maximum 160 Unicode
           codepoints). It is durable and is inherited by resume unless an
           explicit replacement or empty string is supplied.
base_branch: the branch to base the worktree on. Defaults to current HEAD: committed
           state only, so commit first if the child needs your uncommitted edits. Not
           with task_kind="independent_review", whose base is the source's pinned commit.
resume:    a Theater participant id, or a harness session id from `recall`, to resume
           instead of starting cold. This is the way back for a dead participant,
           killed, crashed or gone: it starts a NEW child (new id; the name is not
           inherited) carrying the old session's context. A participant id wins over
           a native session id; it must be dead with a recorded session_id, and a
           live one is refused (use send). list_participants `resume_state` says
           whether a retained row can resume. Some harnesses accept resume but cannot
           carry a prompt through it: opencode's `-s` drops `--prompt`, so resume it
           without a prompt and use send to deliver the task; response_format is
           refused for them too, as it travels the same channel. Resume is also
           refused for live trusted owners and for `transcript_identity_lost`;
           recover by inspecting `theater candidates <id>` and rebinding with
           `theater bind <id> <candidate> --confirm-id <id>`.
wiring:   "auto" | "native" | "legacy" — how the child's controls are wired.
           "auto" follows the daemon's compatibility checks and rollout
           gate. "native" prefers a compatible runtime and uses the bound
           terminal provider for capabilities outside that runtime; "legacy"
           opts out of native runtime wiring but still requires that provider.
           Routes are chosen per capability, preserving the harness's native UI.
           The choice changes nothing about `approval`, which stays required.

The returned participant record includes `session_id`, the harness's opaque
resume identifier, only after the observer discovers it; a new spawn normally
omits the field. With neither prompt nor response_format, the launch job becomes
done only after successful terminal binding. Otherwise terminal creation is not
task completion, and an unmatched turn may leave the job running. Check
`operation_state`: only `succeeded` confirms terminal creation. Otherwise
await_sessions([handle]) settles it (the job ends crashed if the launch fails) and
list_participants(ids=[id]) shows the child once it is addressable; spawn no
replacement meanwhile.

Refused with depth_exceeded past the depth cap (default 3 below the root) and with
budget_exceeded once your whole tree holds the budget of live participants (default
20; dead ones free their slot). Then do the work yourself, or ask the user to retire
children whose work is merged.
"""


def _spawn_description() -> str:
    """SPAWN_DOC with the harness names of this process's registry filled in.

    Every registered adapter is named, including one whose binary is missing:
    `installed` here would be resolved against *this* process's PATH, and the
    daemon spawns with its own. Naming a harness the daemon can in fact spawn
    matters more than hiding one it cannot — the second case fails at the call
    with a message that says so, the first fails silently by never being tried.
    """
    names = [row["name"] for row in describe() if not row["error"]]
    return SPAWN_DOC.format(harnesses=", ".join(names) or "no harnesses registered")


def _register_runtime_controls(mcp: MCPServer, mcp_tool, session: Session) -> None:
    """Register the runtime-control tools (settings, controls).

    Every one of them is a thin forwarder: the daemon authorizes from the
    forwarded caller identity and its refusal reason is the answer, so nothing
    is decided here. They belong to the control lane, never to the wait lane.
    """

    @mcp_tool()
    async def cancel_job(handle: str) -> dict:
        """Withdraw work you requested that has not started.

        A queued message Theater has not transmitted, or a spawn_session still waiting for
        its `after` source (nothing was created for it). Only the requester can cancel.
        Returns {"handle", "cancelled"}; when it is too late, `reason` is "delivered",
        "delivery_uncertain" (it may have reached the target or be launching; never assume
        it did not), "finished" (with `state`), or "not_cancellable".

        Too late? A direct child's work, including a launched spawn's task, stops with
        interrupt_session (or send(if_busy="replace")); removing the child itself takes
        terminate_session, after asking the user. A message a peer already received cannot
        be withdrawn: send it a correction.
        """
        return await tools.cancel_job(session, handle=handle)

    @mcp_tool()
    async def update_session_settings(
        target: str, model: str | None = None, reasoning_effort: str | None = None
    ) -> dict:
        """Change a direct child's model or reasoning effort while it is idle.

        Idle-only: the daemon refuses while the child has an active turn or a
        pending native interaction — call get_session_controls to see the
        active turn first. Supply at least one of `model` and
        `reasoning_effort`; both follow the same allowlists spawn_session
        enforces (list_harnesses(include_models=true) reports them). Approval and sandbox policy are
        never changed here. Effective values are reported only after the
        native backend confirms; an uncertain delivery stays visibly
        uncertain in the reply.

        target: the child's stable participant id or current live name.
        model: optional new model, spelled the way its CLI spells it.
        reasoning_effort: optional new reasoning effort.

        Settings updates require the direct parent or a local operator; the
        daemon decides from the forwarded caller identity.
        """
        return await tools.update_session_settings(
            session,
            target=target,
            model=model,
            reasoning_effort=reasoning_effort,
        )

    @mcp_tool()
    async def get_session_controls(target: str) -> dict:
        """Diagnose why a send or setting is not going through: one session's controls.

        Read-only — nothing is delivered or changed. The daemon answers
        with the effective capabilities (send, steer, queue_followup,
        settings_update, interrupt), its own reason for each unavailable
        one, the control connection's health, the effective settings, the
        active turn, and the handles of queued followups. Call this before
        send(if_busy=...) or update_session_settings to learn whether the
        harness supports the operation at all; when it does not, the reason
        is the daemon's, and it is the thing to tell the user rather than
        retrying. Empty fields are omitted, and a transport or runtime_host shared by
        several capabilities is lifted into `capability_route`. `active_turn` and
        `settings` are reported only while a natively wired session's runtime is
        connected; elsewhere they are absent even mid-turn, so read status from
        list_participants. Steering (if_busy="amend") and settings updates need native
        wiring, chosen at spawn.

        target: the participant's stable id or current live name.
        """
        return await tools.get_session_controls(session, target=target)


def _register_lifecycle_tools(mcp_tool, session: Session) -> None:
    """Register terminate and adopt: one explicit-approval rule, two ends of a child's life."""

    @mcp_tool()
    async def terminate_session(target: str, forget: bool = False) -> dict:
        """Permanently kill one direct child session.

        **Before killing any child, ask the user for permission and wait for an
        explicit yes.** You, not the user, must name every direct child you intend
        to kill in that request (use your native ask-user tool if you have one). A
        bare `yes` or `confirm` authorizes every child you named; never ask the user
        to repeat the names. After confirmation, make all authorized calls before
        replying. An earlier approval, approval to spawn, or a vague request to clean
        up is not enough. Do not bypass this by invoking `theater kill` in a shell.

        Exception: a child holding a position your play lists as disposable (`whoami`
        `play.disposable`) may be retired without asking once its result is collected and
        its commit merged or dropped; check its `play.position` (`list_participants`) first.

        target: live name (unique) or id. A name you just saw is safe; after
        a death it may be recycled, so use the id for a child you have not
        seen alive since. Only your direct children are eligible. If the requested
        cleanup includes deeper descendants, explain that ownership limit and
        coordinate cleanup through their direct parents; do not claim that the
        user's confirmation syntax is the problem.

        forget: true retires a child with no terminal, route, or live process
        (a ghost) without terminal I/O; anyone may forget an orphan whose owners
        are dead. Refusals (e.g. `human_present`) carry `next`. An already-dead
        child returns {"killed": false, "reason": "already_dead"}.

        For `worktree=True`, verified termination also cleans up the unique
        worktree and deletes its merged branch. Dirty or still-used worktrees
        and unmerged branches are retained. `workspace_cleanup` reports the
        operation and any partial result; pending cleanup can be inspected with
        `theater workspaces get <workspace_id>`.

        Named shared worktrees and their branches are retained for explicit
        cleanup. `theater workspaces cleanup <workspace_id> --delete-branch`
        removes a clean, unused worktree and its merged branch. Cleanup without
        `--delete-branch` retains the branch; force flags are separate choices.
        A child without a worktree has no worktree or branch to delete.

        The kill cannot be undone, but the child's session context survives:
        spawn_session(resume=<its id>) starts a new child from it while
        list_participants(include_dead=true) shows its resume_state as resumable.
        """
        return await tools.terminate_session(session, target=target, forget=forget)

    @mcp_tool()
    async def adopt_session(target: str) -> dict:
        """Adopt one sibling participant as your own direct child.

        **Before adopting any participant, ask the user for permission and
        wait for an explicit yes, exactly like terminate_session.** You, not the
        user, must name every participant you intend to adopt in that request (use
        your native ask-user tool if you have one). A bare `yes` or `confirm`
        authorizes every participant you named; never ask the user to repeat the
        names. After confirmation, make all authorized calls before replying. An
        earlier approval, approval to spawn, or a vague request to reorganize the
        team is not enough. Do not bypass this through a shell, a CLI, or any other
        client.

        Only a sibling qualifies: a participant under the same parent as you —
        two roots count as sharing a parent. The target must be live,
        addressable (a verified terminal-provider or native-runtime route),
        and still owned by your shared parent — or, when you are both roots, by
        the local operator. If control was transferred to a third participant,
        that owner must release it first. After adoption the target is
        reparented under you, its queued (not dispatched) followups are
        cancelled, and you become its current control owner: every gate keyed
        on "control_owner_id or parent_id" — kill, interrupt, steer,
        queue_followup, settings updates — now belongs to you, and your own
        parent no longer owns it. The target's own children come along with it:
        they keep their parent and only gain a generation of depth.

        target: live name (unique) or id; names of dead participants do not
        resolve and may be recycled.

        Refuses with `self_adopt`, `dead_target`, `not_addressable`,
        `not_sibling`, `ownership_conflict`, `busy` (a termination is in
        flight for the caller or the target), `depth_exceeded`,
        `cycle_detected`, `budget_exceeded`, or `not_found`.
        """
        return await tools.adopt_session(session, target=target)


def _client_lifespan(session: Session):
    @asynccontextmanager
    async def lifespan(server):
        try:
            yield
        finally:
            await session.client.aclose()

    return lifespan


class _TheaterServer(MCPServer):
    """Refusals become envelopes in the harness's delivery mode; unknown arguments are named."""

    def __init__(self, *args: Any, error_delivery: ErrorDelivery, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.error_delivery: ErrorDelivery = error_delivery
        #: tool name -> accepted parameter names
        self.tool_parameters: dict[str, frozenset[str]] = {}

    async def call_tool(self, name: str, arguments: dict[str, Any], context=None):
        try:
            result = await super().call_tool(name, arguments, context)
        except Exception as exc:
            envelope = refusal_envelope(exc)
            if envelope is None:
                raise
            return refusal_result(envelope, self.error_delivery)
        accepted = self.tool_parameters.get(name)
        if accepted is None or not isinstance(result, CallToolResult):
            return result
        result = with_ignored_arguments(result, ignored_arguments(arguments, accepted))
        return with_structured_object(result, tool_name=name)


def build(
    participant_id: str | None = None,
    harness: str = "unknown",
    toolset: str = "all",
) -> MCPServer:
    if toolset not in _TOOLSETS:
        choices = ", ".join(sorted(_TOOLSETS))
        raise ValueError(f"unknown MCP toolset {toolset!r}; expected one of: {choices}")
    session = Session(
        participant_id=participant_id or os.environ.get("THEATER_ID"),
        harness=harness,
        client=DaemonClientPool(),
    )
    mcp = _TheaterServer(
        HARNESS_MCP_SERVER_NAME,
        instructions=_instructions(toolset),
        lifespan=_client_lifespan(session),
        error_delivery=mcp_error_delivery(harness),
    )
    tool_names: set[str] = set()

    def mcp_tool(*, description: str | None = None):
        """Register a tool when it belongs to this lane."""

        def decorator(fn):
            if _includes_tool(toolset, fn.__name__):
                tool_names.add(fn.__name__)
                mcp.tool_parameters[fn.__name__] = frozenset(inspect.signature(fn).parameters)
                # cleandoc: indented docstrings would otherwise ship their margin on every line.
                doc = description if description is not None else fn.__doc__
                return mcp.tool(description=inspect.cleandoc(doc) if doc else None)(fn)
            return fn

        return decorator

    @mcp_tool()
    async def whoami() -> dict:
        """Identify yourself. Returned fields include id, live name, description,
        harness, tier, status, cwd, branch, session_id, parent_id, addressable,
        and play inside a live play; unset fields are omitted. play holds play,
        position, master and spawn_options: each child position you may give,
        with the task roles that fit it ([] when you may spawn none), and
        disposable: the positions you may retire without asking the user. Under a
        live play without a position of your own, position is null, unpositioned
        is true and spawn_options is []: you cannot spawn until your direct
        parent sends you a position. Includes live direct children
        (name, status, harness; first 20, children_total beyond).

        Call this before addressing anyone else, so you can tell yourself apart
        from the other participants in the list. `session_id` is the harness's
        opaque resume identifier and is omitted until Theater's observer finds
        your transcript; call this tool again later to refresh it.
        """
        return await tools.whoami(session)

    @mcp_tool()
    async def list_participants(
        include_dead: bool = False,
        ids: list[str] | None = None,
        children_only: bool = False,
        limit: int | None = None,
        after_id: str | None = None,
    ) -> list[dict]:
        """List every agent on this machine that Theater knows about.

        Each entry says who they are (harness, id, session_id, and play while
        they hold a live play position), where they are
        (cwd, branch), how they got here (tier, parent_id), and whether Theater
        currently has a verified physical inbound route (`addressable`). That
        field is not a readiness or permission verdict. `session_id` is the
        harness's opaque resume identifier; it is omitted until Theater's
        observer discovers the transcript, so list again later if you need it.
        Origin alone does not decide addressability: an external participant is
        addressable only while an independent delivery route is verified.

        Names are unique among live participants and every tool's `target`
        accepts one, so prefer names for live agents. They last while the
        participant lives, daemon restarts included: a dead participant has no
        name (the field is omitted) and a name may be recycled after a death,
        so use the id for dead rows, resume, and any reference you keep across a
        death. Ids stay valid until retention GC.
        Empty fields are omitted from every row; so are `is_self` (present, true, only on
        your own row), `resume_state` when it is "live", and `addressable` on dead rows.

        Historical rows may retain `tmux_server_identity`, but it is not a live
        route. A dead row may carry `termination_reason`, `termination_incident`,
        and `terminated_at` for retained history.

        `ids` is an optional list of participant ids to fetch — real ids only,
        not names (names are live-only, recyclable aliases). Pass it when you
        need a small number of rows and do not want to pay for the whole table.
        An empty list returns an empty result immediately. Unknown ids are
        silently omitted; diff the requested list against the returned rows if
        you need to know which were absent. `ids` composes with `include_dead`:
        a dead participant is only returned when `include_dead=True`, even when
        its id is listed explicitly.

        Set `children_only=True` to list only participants spawned directly by
        you. It excludes deeper descendants and composes with both `ids` and
        `include_dead`.

        `include_dead=True` lists newest first, 20 rows by default. Pass `limit`
        (1 through 200) to choose a page size; a full page's last row carries
        `next_after_id`, which you pass as `after_id` for the next page.
        Live-only lists are oldest first. If a cursor was deleted by retention
        GC, restart from the first page. `limit` caps an `ids` query;
        `after_id` cannot be used with `ids`.

        Dead rows carry `resume_state`, the verdict `spawn_session(resume=...)` checks:
        `resumable` (every gate passed), `owned_by_live` (a live participant already
        continues that session: address it), `no_session_id`, `harness_cannot_resume`,
        `untrusted` (needs operator binding) or `harness_resume_rejected`.
        """
        return await tools.list_participants(
            session,
            include_dead=include_dead,
            ids=ids,
            children_only=children_only,
            limit=limit,
            after_id=after_id,
        )

    @mcp_tool()
    async def list_harnesses(
        harness: str | None = None, include_models: bool = False
    ) -> list[dict]:
        """The CLIs you can pass to spawn_session as `harness`, on this machine.

        The daemon answers; the set is configuration, not a fixed list. Rows carry
        `name` and `approvals` (null means the daemon predates the field: restart
        it), plus `binary` only when it differs from the name.
        harness: list only this one.
        include_models: add, per harness, the model allowlist spawn_session
        enforces: `models`, `supported`, `reasoning` and `reasoning_supported`.
        The allowlist is written by a human in Theater's config, not everything the
        CLI could run, and it starts empty: `models: []` refuses every model you
        name, and `supported: false` means the adapter cannot select a model at
        all, so no config edit helps; suggest the edit rather than retrying with
        another name. Omitting `model` bypasses the allowlist. `reasoning` is the
        allowlist of efforts, with the same semantics; `reasoning_supported: false`
        means the adapter cannot select one.
        """
        return await tools.harnesses(session, harness=harness, include_models=include_models)

    @mcp_tool()
    async def list_skills(play: str | None = None) -> dict:
        """List skill metadata first, before choosing one to load.

        Returns standalone skills, then plays: agent organizations whose members
        are named <play>/<member>. Each play lists its master skill, its positions
        (with the positions each may spawn) and its task roles (with the positions
        they fit). No instruction content; rejected packages are reported. Pass
        play to list one play only. Choose an exact name before calling load_skill.
        Check it whenever the user's request names a workflow, before building that
        workflow from raw tools.
        """
        return await tools.list_skills(session, play=play)

    @mcp_tool()
    async def load_skill(name: str) -> dict:
        """Load the full instructions for one exact skill selected from list_skills.

        Loading returns the complete validated SKILL.md content and consumes
        context, so list metadata first and load only the selected exact skill.
        Loading a play's master skill starts that play with you as master
        (play_start reports it) and commits you to it: it lasts as long as you
        live, and every spawn you make attaches one position your own position
        may give (whoami: play.spawn_options). Load a master skill only when the
        user asked for that play. Inside a live play, or above one, it returns the text without
        starting anything.
        """
        return await tools.load_skill(session, name=name)

    @mcp_tool(description=_spawn_description())
    async def spawn_session(
        harness: str,
        approval: str,
        prompt: str | None = None,
        response_format: dict | None = None,
        cwd: str | None = None,
        worktree: str | bool | None = False,
        base_branch: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        resume: str | None = None,
        name: str | None = None,
        description: str | None = None,
        wiring: Literal["auto", "native", "legacy"] = "auto",
        after: str | None = None,
        task_kind: Literal["work", "independent_review"] | None = None,
        skills: list[str] | None = None,
    ) -> dict:
        return await tools.spawn_session(
            session,
            harness=harness,
            prompt=prompt,
            response_format=response_format,
            approval=approval,
            cwd=cwd,
            worktree=worktree,
            base_branch=base_branch,
            provider=provider,
            model=model,
            reasoning_effort=reasoning_effort,
            resume=resume,
            name=name,
            description=description,
            wiring=wiring,
            after=after,
            task_kind=task_kind,
            skills=skills,
        )

    @mcp_tool()
    async def update_participant(
        target: str | None = None,
        name: str | None = None,
        description: str | None = None,
    ) -> dict:
        """Update your metadata or one direct child's metadata.

        `target` defaults to yourself and accepts a stable participant id or a
        current live name. You may update only yourself or a direct child;
        dead targets are refused. Supply at least one field. `name` is a
        case-insensitively unique alias, kept while the target lives, and cannot
        be cleared.
        `description` is durable; use an empty string to clear it, or a short,
        plain, specific, non-verbose single-line summary up to 160 Unicode
        codepoints. Keeping your own description aligned with the task at hand
        is highly recommended. If a parent supplied an accurate description,
        leave it unchanged. Set it when missing, and update it only when the
        task materially changes or it becomes inaccurate.
        """
        return await tools.update_participant(
            session,
            target=target,
            name=name,
            description=description,
        )

    ceiling = await_ceiling(harness)

    @mcp_tool(description=_await_doc(ceiling))
    async def await_sessions(
        handles: list[str], max_wait: float = RPC_DEFAULT_MAX_WAIT_SECONDS
    ) -> list[dict]:
        """Forward bounded, presence-aware wait-any to the daemon."""
        return await tools.await_sessions(
            session, handles=handles, max_wait=max_wait, ceiling=ceiling
        )

    @mcp_tool()
    async def send(
        target: str,
        prompt: str,
        if_busy: Literal["auto", "refuse", "queue", "amend", "replace"] = "auto",
        job_handle: str | None = None,
        response_format: dict | None = None,
        after: str | None = None,
        skills: list[str] | None = None,
    ) -> dict:
        """Send a prompt to an already-running agent mid-session.

        Delivery follows the daemon's selected capability route: native runtime
        or the bound terminal provider. The route is chosen before an attempt;
        potentially accepted delivery is never replayed or failed over. The
        target must be addressable: that means a verified physical inbound route,
        not readiness or permission. Ordinary send is not ownership-gated. The
        returned handle can be awaited. One sender's messages to one target
        arrive in the order sent; other senders' messages are not held behind them.

        target:    live name (unique among live participants) or id. Names
                   work only while the participant is live and may be
                   recycled after a death.
        prompt:    the text to deliver to the target.
        if_busy:   omit it. The default "auto" delivers once when safe and otherwise
                   queues the message behind the target's work (or a human at its
                   terminal), returning one handle either way; delivery="queued"
                   marks a message not yet sent, and cancel_job withdraws it.
                   "refuse" is open to anyone and fails busy or human_present
                   instead of queueing. The other explicit modes need a direct
                   child: "queue" queues when busy (route "queued"); "amend" steers
                   the active turn (route "steer"), else queues; "replace" cancels
                   its queue, interrupts, waits up to 30 s for idle, sends (route
                   "replace"). A participant cannot queue a message to itself.
        job_handle: amend/replace act only while this job is the active turn.
        response_format: optional JSON object, with the same contract as spawn_session
                   (presets, custom schemas, structured_result in await_sessions).
        after:     a live participant name or an exact job handle. A name means the work
                   that agent has already started, never a message still queued for it:
                   if your send returned delivery="queued", pass that send's handle. If
                   that work is running, the message is queued (delivery="queued",
                   handoff.state="waiting") until it ends. If already done, the message
                   may deliver immediately. Theater adds
                   a labelled card: its outcome, pinned Git commit and
                   dirty state, and its answer marked unverified (a failed source gives a
                   labelled failure). You write the task; copy no path or handle. The same
                   handle covers the wait and the reply; cancel_job withdraws it while it
                   waits; an await on it from the source's own work is a cycle, refused.
                   Presence is still checked when it is delivered. Omit if_busy with it.
        skills:    optional skill names from list_skills, attached after this message's
                   task as for spawn_session; they apply to this message only. Refused
                   with if_busy="amend" while a turn is active. Play skills need the
                   target's live play: a position sent by a direct parent to a child
                   without one becomes its position once Theater confirms delivery,
                   and a role must fit the target's position. If the play ends or the
                   position no longer fits first, the queued message fails unsent with
                   `play_refused`. If the child still has no `play` position after the
                   job ends, send the position again.
        Fails with `busy` for if_busy="refuse", a target whose queue (or your share of
        it) is full, or a send to yourself while you are working; with
        `transcript_untrusted` or `transcript_identity_lost` (transcript needs
        binding); with `human_present` for if_busy="refuse" or a send to yourself; and
        with `awaiting_decision` for if_busy="refuse" or "amend" while the target has an
        open approval, trust or question dialog that only a human can answer.
        """
        return await tools.send_prompt(
            session,
            target_id=target,
            prompt=prompt,
            response_format=response_format,
            if_busy=if_busy,
            job_handle=job_handle,
            after=after,
            skills=skills,
        )

    @mcp_tool()
    async def interrupt_session(target: str) -> dict:
        """Cancel one direct child's active turn and undelivered followups.

        Two things are cancelled, in this order: every queued followup of
        the child that has not been delivered yet — those handles finish
        killed with error_code "interrupted" and can be queued again if the
        work is still wanted — and then the active turn itself. If the child
        has already stopped working, this is a harmless no-op that still
        clears the queue.

        `target` accepts the child's stable participant id or current live
        name. The child must be addressable. This can discard an in-progress
        response or tool call. It does not kill the participant, close its
        terminal, delete its workspace, change its status directly, or wait for
        confirmation; the observer remains the authority on when the child
        becomes idle. Delivery follows the daemon's route for interrupt:
        the native runtime or the bound provider's declared terminal sequence
        — never one universal key assumed to work everywhere — and Theater
        refuses to mutate a terminal a human is using. To interrupt and send a
        replacement in one call, use send(if_busy="replace").

        Returns {"id": str, "interrupted": bool, "interrupted_job"?: handle,
        "reason"?: str, "cancelled_followups"?: [handle, ...]}; cancelled_followups
        is omitted when empty, and reason is an open set. Refusals are non-exhaustive and
        include `human_present`, `busy`, and `stale_target`. Only the direct
        parent or a local operator may interrupt; the daemon decides from the
        forwarded caller identity.
        """
        return await tools.interrupt_session(session, target=target)

    _register_runtime_controls(mcp, mcp_tool, session)

    @mcp_tool()
    async def scratchpad_write(value: str, namespace: str, key: str | None = None) -> dict:
        """Write a small machine-wide TTL-bound scratchpad entry; daemon mints the key.

        Returns {"namespace": str, "key": str}. The key is a random short id
        unless you pass one, in which case that entry is updated if it exists
        or inserted if it does not. The namespace and key are global to this
        Theater machine, not a Git tree or participant lineage. Every successful
        write sets the configured expiry; reads never extend it.

        Use it for small coordination facts: file claims, handoff notes,
        shared design decisions, or breadcrumbs. Do not use it for mutual
        exclusion, queues, durable memory, or source-of-truth records.

        Bounded: one value carries at most 256 KiB of raw UTF-8; a namespace
        holds at most 512 live entries and 1 MiB of raw UTF-8 keys plus values.
        On writes, namespace and supplied key are limited to 128 characters.
        Store a pointer or summary, not the payload itself.

        value:     exact string to store.
        namespace: coordination bucket chosen by the agents sharing it.
        key:       optional key to update or insert under; None mints a new id.
        """
        return await tools.scratchpad_write(
            session,
            value=value,
            namespace=namespace,
            key=key,
        )

    @mcp_tool()
    async def scratchpad_get(
        namespace: str,
        keys: list[str] | None = None,
        after_key: str | None = None,
        keys_only: bool | None = None,
    ) -> dict:
        """Read non-expired entries from the machine-wide TTL scratchpad.

        Without `keys` it lists metadata: {"namespace", "keys": [...], "entries": [{key,
        bytes, updated_at, expires_at, actor}, ...], "truncated"}, so a scan never pulls
        values you did not ask for. With `keys` (at most 128) it returns
        {"entries": {key: value, ...}, "truncated"}; keys_only=false forces values
        for a scan, keys_only=true forces metadata for specific keys. Entries are ordered
        by key. One page returns at most ~4 MiB encoded; when truncated is true and entries
        were returned, an `after_key` is present: pass it to continue after the last key. A
        legacy entry too large to return reports oversized_digest, oversized_bytes and a
        nullable oversized_key: skip it with after_key only when non-null, else stop and
        report it; delete by digest or clear only when cleanup is intended.
        Entries are shared by namespace across this Theater machine; expired
        entries are omitted and reads never refresh their expiry.

        namespace: coordination bucket chosen by the agents sharing it.
        keys:      optional list of at most 128 entry ids, each at most 128
                   characters; None means all.
        after_key: optional cursor with no length limit; return keys after it.
        keys_only: true lists metadata only; false returns values. Default: metadata when
                   `keys` is omitted, values when it is given.
        """
        return await tools.scratchpad_get(
            session, namespace=namespace, keys=keys, after_key=after_key, keys_only=keys_only
        )

    @mcp_tool()
    async def scratchpad_delete(
        namespace: str,
        keys: list[str] | None = None,
        digests: list[str] | None = None,
        clear: bool = False,
    ) -> dict:
        """Delete scratchpad entries by key or oversized-entry digest, or clear
        every entry in the namespace.

        keys and digests may be combined, with at most 128 entries in each.
        digests are the 64-character lowercase oversized_digest values from
        scratchpad_get. Supply at least one key or digest, or clear=true with
        neither. Keys have no length limit so legacy entries remain deletable.

        Returns {"namespace": str | null, "deleted": [key, ...],
        "deleted_oversized"?: [digest, ...]}, or {"namespace": str | null,
        "deleted_count": int} for clear. A legacy namespace too large to echo
        returns null.
        """
        return await tools.scratchpad_delete(
            session, namespace=namespace, keys=keys, digests=digests, clear=clear
        )

    @mcp_tool()
    async def read_transcript(
        target: str, cursor: str | None = None, roles: list[str] | None = None
    ) -> dict:
        """Diagnose a child: read a bounded transcript page, newest first.

        For a stuck, failed or surprising child only; await_sessions already returns the
        answer. Call once with a known live name or stable id, inspect the newest
        bounded chunk, and use only the returned next_cursor when older
        content is necessary. Stop when the needed event is found; do not call
        list_participants first.

        cursor: opaque next_cursor returned by Theater, or null for the
        newest page. Never construct or edit a cursor.
        roles: any of assistant, user, tool_call, tool_result;
        ["assistant"] reads the last answer.

        Returns id, path, events (chronological), next_cursor, and truncated.
        truncated means the response budget cut this source page before all eligible
        events were returned. An event carries role, plus text, tool_name, turn_end and
        turn_terminal when set; empty values are omitted, and a finished answer that
        Theater also recorded as its seal appears once. A partial text chunk also
        carries index and UTF-8 byte offsets (text_start_byte, text_end_byte,
        text_total_bytes). Events over 64 KiB return head text, tail, elided_bytes,
        middle_cursor (pages the elided middle). The newest unfiltered page adds
        first_prompt (earliest retained job prompt, omitted when already in the
        events), later_prompts, resumed_from when known; filtering by roles omits them.
        Which job edited a file: recall(paths), then git diff in the child's branch.

        Refuses if the transcript needs binding or its identity is lost.
        """
        return await tools.read_transcript(session, target=target, cursor=cursor, roles=roles)

    _register_lifecycle_tools(mcp_tool, session)

    @mcp_tool()
    async def recall(paths: list[str], depth: int = 5) -> dict:
        """Who last changed these files, when, and on whose orders.

        Reach for this before editing a file you did not write in this
        session, when a change you cannot account for shows up, or when
        you need to reach the agent behind one. It reads Theater's own
        record of every job that touched the path, so it answers where
        ``git log`` cannot: uncommitted edits, and the identity of the
        agent that made each change.

        One timeline per path, newest first. Each point carries clipped
        task/result (the requester's task, without Theater's response_format
        guidance), outcome, one `sha` string (`before → after`, with
        `-` for missing and `?` for unavailable), a ``segment`` that ``recall_read``
        expands, and, for job points, `participant_id` and its live `name` (null once
        it died): send to the name while it lives, or spawn_session(resume=<id>).
        Beside the timeline: ``current`` (the file's sha now) and ``dirty`` (differs
        from HEAD). Compare ``current`` with the newest point's ending SHA to detect
        post-job changes.

        A gap point is a sha transition no job claims — something
        outside Theater edited the file. ``recall_read`` on its segment
        asks git what.

        When `resume` is true, `session_id` may be passed to
        ``spawn_session(resume=<session_id>)`` to attempt restoring the original
        context; current launch gates may still refuse it. When false,
        `resume_note` explains why.

        paths: repo-relative or absolute within your repository. Outside paths
               and traversal are rejected.
        depth: points per path, gaps included. Default 5.

        Scoped to your git root. Worktree children live under it, so their
        edits appear — two worktrees sharing a repo-relative path share
        one timeline, and the jump between them can read as a gap.
        """
        return await tools.recall(session, paths=paths, depth=depth)

    @mcp_tool()
    async def recall_read(segment_id: str) -> dict:
        """Open one point of a recall timeline: bounded details behind it.

        Call this when a point's clipped `task`/`result` is not enough
        and you want the agent's actual reasoning, or when a gap point
        needs explaining. Pass the `segment` value from the point.

        Job segment: the newest bounded transcript page, plus touched paths
        and their shas. Truncation is explicit; use `read_transcript` to page
        older material. A missing transcript is reported as unavailable;
        the job's retained metadata still arrives.

        Gap segment: the commits git can attribute to the transition.
        `explained: false` means no explaining commits were returned; it does
        not prove the edit was never committed. Missing repository context,
        rejected paths, and git failures can also produce it.

        The only Theater call that spends a `git log`, and it answers
        about one segment: let `recall` narrow first, then spend this on
        the point that matters.
        """
        return await tools.recall_read(session, segment_id=segment_id)

    mcp.middleware.append(ToolTiming(frozenset(tool_names), toolset))
    return mcp


def main(
    participant_id: str | None = None,
    harness: str = "unknown",
    toolset: str = "all",
) -> None:
    build(participant_id, harness, toolset).run(transport="stdio")
