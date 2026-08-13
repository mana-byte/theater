"""Whose turn was that, and therefore whose question it answers.

Naming a turn (test_turn_identity.py) says *which* turn ended. This says who
caused it, which is a different question with a worse failure mode.

The bug: a human types into a pane that has a peer's job waiting on it. The
harness writes an ordinary turn — user record, reply, boundary — the observer
sees a boundary with a job running, and resolves the job. The peer receives
the operator's private conversation as the answer to a question it asked, and
its actual answer, when it arrives, resolves nothing at all.

The fix rests on one empirical fact, and the fixtures in test_turn_identity
are what establish it: all four harnesses echo an injected prompt back as a
user record, verbatim, before the reply. So the user text a turn opens with
identifies the turn. That is checked here against those same real captures,
because the day a harness stops echoing is the day this gate starts hanging
every send to that harness — a failure worth catching in CI rather than at
2am with two agents deadlocked.

The gate is deliberately one-sided: it withholds an answer only on positive
evidence of somebody else's prompt. No user record at all means answer, which
covers attaching mid-turn, screen-derived boundaries, and any harness that
keeps no user record. See `answers_prompt`.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from shipped import ClaudeCodeObserver, CodexObserver, VibeObserver

from theater.daemon.jobs import JobManager
from theater.daemon.observer import (
    _PROMPT_MATCH,
    UNDELIVERED_CODE,
    Observer,
    QuietClock,
    TurnAccumulator,
    answers_prompt,
)
from theater.harness.base import Event, EventKind, clip
from theater.harness.source import Batch

FIXTURES = Path(__file__).parent / "fixtures"

PROMPT = "please summarise the auth module"


def poised(registry, prompt=PROMPT, *, jobs_wanted=1):
    """A participant with `jobs_wanted` callers queued behind it.

    Same shape as its namesake in test_turn_identity, but the jobs carry a
    prompt: attribution has nothing to work with otherwise, and a promptless
    job is exactly the case that must keep answering unconditionally.
    """
    jobs = JobManager(registry.store)
    p = registry.register(harness="claude", pane="%1", cwd="/tmp")
    for n in range(1, jobs_wanted + 1):
        jobs.create(
            handle=f"h{n}",
            caller_id=f"caller{n}",
            target_id=p.id,
            kind="send",
            prompt=prompt,
        )
    return Observer(registry, harnesses={}, jobs=jobs), p, jobs


def said(text: str, turn_id: str | None = "m1") -> Event:
    return Event(kind=EventKind.ASSISTANT, text=text, turn_end=True, turn_id=turn_id)


def heard(text: str) -> Event:
    return Event(kind=EventKind.USER, text=text, turn_end=False)


# ---- the bug this exists for -------------------------------------------


def test_a_human_typing_does_not_answer_the_waiting_peer(registry):
    """The operator asks their own question while a peer's job is running.

    Before the gate, the peer's `await` returned "the SSO cutover is friday" —
    an answer to a question it never asked, and the operator's text disclosed
    to another agent. Both are unrecoverable once sent.
    """
    observer, p, jobs = poised(registry)
    batch = Batch(
        events=[
            heard("hey, when is the sso cutover?"),
            said("the sso cutover is friday"),
        ]
    )

    observer._apply(p.id, batch, QuietClock(), TurnAccumulator())

    assert str(jobs.get("h1").state) == "running"
    assert jobs.get("h1").result is None


def test_the_peers_own_reply_still_answers_it(registry):
    """The other half. A gate that never opens is not a fix.

    The prompt comes back as a user record, the reply follows, and the job
    resolves exactly as it did before any of this existed.
    """
    observer, p, jobs = poised(registry)
    batch = Batch(events=[heard(PROMPT), said("it validates the bearer token")])

    observer._apply(p.id, batch, QuietClock(), TurnAccumulator())

    assert jobs.get("h1").result == "it validates the bearer token"


def test_the_peer_is_answered_after_the_human_has_had_a_turn(registry):
    """The human's turn is skipped, and the next one is not.

    The realistic sequence: the operator interrupts, the agent works through
    the queue, and the injected prompt is answered a turn later. The job must
    survive the first boundary and be resolved by the second.
    """
    observer, p, jobs = poised(registry)
    clock, turns = QuietClock(), TurnAccumulator()

    observer._apply(
        p.id,
        Batch(events=[heard("wait, what branch am i on?"), said("main", "m1")]),
        clock,
        turns,
    )
    assert str(jobs.get("h1").state) == "running"

    observer._apply(
        p.id,
        Batch(events=[heard(PROMPT), said("it validates the bearer token", "m2")]),
        clock,
        turns,
    )

    assert jobs.get("h1").result == "it validates the bearer token"


def test_a_split_human_turn_is_skipped_on_both_records(registry):
    """Claude announces one message twice, and the second must not slip past.

    The subtle one. `take()` empties the accumulator at the first boundary, so
    the duplicate arrives with no user text — which reads as no evidence, and
    no evidence answers. The turn is marked handled whether or not it was
    answered precisely to close this.
    """
    observer, p, jobs = poised(registry)
    batch = Batch(
        events=[
            heard("what did i name that lock file?"),
            said("theater.lock", "msg_01"),
            said("theater.lock", "msg_01"),
        ]
    )

    observer._apply(p.id, batch, QuietClock(), TurnAccumulator())

    assert str(jobs.get("h1").state) == "running"


def test_one_unmatched_turn_end_leaves_the_job_running(registry):
    """A single miss is legitimate — the human's turn is ahead of ours.

    The prompt is genuinely still queued, and the next turn may answer it.
    The job survives one miss, matching the documented off-by-one.
    """
    observer, p, jobs = poised(registry)
    clock, turns = QuietClock(), TurnAccumulator()

    observer._apply(
        p.id,
        Batch(events=[heard("what branch am i on?"), said("main", "m1")]),
        clock,
        turns,
    )

    assert str(jobs.get("h1").state) == "running"
    assert jobs.get("h1").error_code is None
    # The counter is tracking the miss but has not released the job.
    assert observer._unmatched.get("h1") == 1


def test_two_consecutive_unmatched_turn_ends_crash_the_job(registry):
    """Two misses mean the prompt never reached the queue.

    The pane processed two other turns while ours supposedly waited — no
    real queue does that. The job is released as CRASHED with
    `UNDELIVERED_CODE`, not DONE: no prompt landed and no answer exists,
    which is the same class of failure as a `send` whose `deliver_text`
    raised. DONE would make the caller read an empty string as "the peer
    replied with nothing", a different and quieter failure than the one
    that happened.
    """
    observer, p, jobs = poised(registry)
    clock, turns = QuietClock(), TurnAccumulator()

    observer._apply(
        p.id,
        Batch(events=[heard("what branch am i on?"), said("main", "m1")]),
        clock,
        turns,
    )
    assert str(jobs.get("h1").state) == "running"

    observer._apply(
        p.id,
        Batch(events=[heard("any merge conflicts?"), said("no", "m2")]),
        clock,
        turns,
    )

    job = jobs.get("h1")
    assert str(job.state) == "crashed"
    assert job.error_code == UNDELIVERED_CODE
    # The counter was cleaned up.
    assert "h1" not in observer._unmatched


def test_a_match_after_one_miss_clears_the_counter(registry):
    """A matching turn resets the miss count, so a later miss starts fresh.

    Without this, a human interjecting once and then again two turns later
    would reach the limit on a sequence that is perfectly normal.
    """
    observer, p, jobs = poised(registry)
    clock, turns = QuietClock(), TurnAccumulator()

    # Miss one: human interjects.
    observer._apply(
        p.id,
        Batch(events=[heard("hold on"), said("ok", "m1")]),
        clock,
        turns,
    )
    assert observer._unmatched.get("h1") == 1

    # Match: the peer's turn.
    observer._apply(
        p.id,
        Batch(events=[heard(PROMPT), said("the answer", "m2")]),
        clock,
        turns,
    )
    assert jobs.get("h1").result == "the answer"
    assert "h1" not in observer._unmatched


def test_the_user_record_may_arrive_in_an_earlier_batch(registry):
    """Polls cut wherever they land, including between prompt and reply.

    The user record and the boundary routinely arrive in different batches, so
    attribution has to live in the accumulator rather than be rebuilt per
    call — the same reason the turn's text does.
    """
    observer, p, jobs = poised(registry)
    clock, turns = QuietClock(), TurnAccumulator()

    observer._apply(p.id, Batch(events=[heard(PROMPT)]), clock, turns)
    observer._apply(p.id, Batch(events=[said("the answer")]), clock, turns)

    assert jobs.get("h1").result == "the answer"


def test_a_turn_with_no_user_record_answers_as_it_always_did(registry):
    """No evidence is not evidence of a human.

    Attaching mid-turn skips the user record with everything else before it.
    Refusing to answer here would hang the caller until rescue on every
    participant Theater adopts, which is a far worse trade than the leak this
    gate closes.
    """
    observer, p, jobs = poised(registry)

    observer._apply(
        p.id, Batch(events=[said("the answer")]), QuietClock(), TurnAccumulator()
    )

    assert jobs.get("h1").result == "the answer"


def test_a_promptless_job_answers_as_it_always_did(registry):
    """A spawn with no prompt has no claim to check.

    It keeps the documented off-by-one where such a job soaks up whatever turn
    comes next, rather than being wedged shut by a gate it cannot pass.
    """
    observer, p, jobs = poised(registry, prompt=None)

    observer._apply(
        p.id,
        Batch(events=[heard("something else entirely"), said("the answer")]),
        QuietClock(),
        TurnAccumulator(),
    )

    assert jobs.get("h1").result == "the answer"


# ---- what counts as the same prompt ------------------------------------


def test_whitespace_differences_do_not_break_the_match():
    """Injection is a paste through tmux and a composer's own reflow.

    Requiring the bytes back unchanged would fail on any prompt containing a
    newline, which is most of them.
    """
    echoed = "review   the\n\n patch   please"

    assert answers_prompt([echoed], "review the patch please")


def test_a_harness_may_wrap_the_prompt_in_its_own_scaffolding():
    """Codex writes an `<environment_context>` block around the conversation.

    Today that lands in a separate record the parser drops, so the echo is
    clean. It is matched as a substring anyway: a harness adding a preamble
    tomorrow should degrade to nothing at all, not to every send hanging.
    """
    assert answers_prompt([f"<context>cwd=/tmp</context>\n{PROMPT}"], PROMPT)


def test_a_prompt_longer_than_the_clip_still_matches():
    """Reported text is cut at `MAX_TEXT`; the prompt on the job is not.

    So the two are never equal for a long prompt, and equality would have
    made this gate fire on exactly the prompts most expensive to re-send.
    """
    prompt = "refactor the observer. " + "context context " * 400
    assert len(clip(prompt)) < len(prompt)

    assert answers_prompt([clip(prompt)], prompt)


def test_two_prompts_sharing_only_a_short_opening_are_not_confused():
    """`Hi!` in common is not the same question.

    The window is 120 characters, which two genuinely different prompts do not
    share by accident.
    """
    a = "Hi! " + "a" * _PROMPT_MATCH
    b = "Hi! " + "b" * _PROMPT_MATCH

    assert answers_prompt([a], a)
    assert not answers_prompt([a], b)


def test_no_user_text_and_no_prompt_both_answer_yes():
    """The two fallbacks, stated as a property rather than a code path."""
    assert answers_prompt([], "anything")
    assert answers_prompt(["anything"], None)
    assert answers_prompt(["anything"], "   ")


# ---- against the real captures -----------------------------------------

CAPTURED = {
    "claude": ClaudeCodeObserver,
    "codex": CodexObserver,
    "vibe": VibeObserver,
}

#: What `theater send` was given when the captures were taken, and therefore
#: what the job row held while the harness was replying to it.
CAPTURE_PROMPT = (
    "Reply with exactly this one line and nothing else, "
    "no tools, no preamble: CAPTURE-{}-OK"
)


@pytest.mark.parametrize("name", sorted(CAPTURED))
def test_each_harness_echoes_the_prompt_it_was_sent(name):
    """The fact the whole gate rests on, checked against one real round-trip.

    `tests/fixtures/turn_*` are verbatim captures taken through the daemon
    against the live CLIs. If a harness upgrade stops echoing the prompt, or
    starts rewriting it beyond recognition, this fails here — where it reads
    as "attribution can no longer see this harness" — instead of in
    production, where it reads as "sends to claude hang for sixty seconds".

    opencode is in test_turn_identity rather than here: its transcript is a
    database and replaying it needs that module's fixture. The property is the
    same and is asserted there.
    """
    observer = CAPTURED[name]()
    path = FIXTURES / f"turn_{name}.jsonl"
    events = [
        e
        for i, line in enumerate(path.read_text().splitlines())
        for e in observer.parse(line, i)
    ]
    sent = CAPTURE_PROMPT.format(name.upper())
    users = [e.text for e in events if e.kind is EventKind.USER]

    assert users, f"{name} reported no user record for an injected prompt"
    assert answers_prompt(users, sent)
    # And is specific enough to reject a different prompt to the same harness.
    assert not answers_prompt(users, CAPTURE_PROMPT.format("SOMETHING-ELSE"))
