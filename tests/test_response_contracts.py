"""Strict response contracts: admission, whole-answer validation, snapshots, fail-closed finish."""

from __future__ import annotations

import json

import pytest

from theater.constants.observation import PROMPT_MATCH
from theater.constants.response_contract import CONTRACT_PRESET_NAMES, STRUCTURED_UNABLE_TO_PARSE
from theater.daemon.jobs import JobManager
from theater.daemon.observation.completion import CompletionTracker
from theater.daemon.observation.turns import answers_prompt
from theater.daemon.persistence.repositories.job_repairs import RepairAttempt, RepairState
from theater.daemon.response_contracts import parse_stored, resolve_response_format
from theater.daemon.rpc.params import _prompt_with_response_format
from theater.models import BadRequest, JobState, now

DELEGATE = json.dumps(
    {
        "status": "done",
        "summary": "Fixed the race in dispatch.",
        "blockers": [],
        "commit": "abc123",
        "changed_paths": ["a.py"],
        "checks": [{"command": "pytest -q", "result": "pass", "detail": "12 passed"}],
    }
)
#: The smallest valid answer of each preset, and an optional nested item to carry an extra key.
MINIMAL = {
    "delegate": {"status": "partial", "summary": "Half done.", "blockers": []},
    "review": {"verdict": "approve", "reviewed": "abc..def", "summary": "Fine.", "findings": []},
    "debate": {
        "verdict": "agree",
        "position": "Ship it.",
        "strongest_evidence": "a.py:3 (observed)",
        "weakest_assumption": "none",
        "position_change": "initial",
        "remaining_blocker": "",
    },
}
NESTED = {
    "delegate": ("checks", {"command": "pytest", "result": "pass"}),
    "review": ("findings", {"severity": "nit", "issue": "typo"}),
    "debate": ("supporting_evidence", {"claim": "it works"}),
}
CUSTOM = {"$theater": "custom", "name": "verdict", "schema": {"enum": ["yes", "no"]}}


def resolve(store, raw, creator="parent"):
    return resolve_response_format(store, raw, creator_id=creator)


# ---- admission ----------------------------------------------------------------


def test_bare_dict_stays_a_hint_and_strict_forms_snapshot_the_schema(store):
    hint = resolve(store, {"type": "object"})
    assert hint == '{"type":"object"}'
    assert parse_stored(hint) is None

    for name in CONTRACT_PRESET_NAMES:
        contract = parse_stored(resolve(store, {"$theater": name}))
        assert contract is not None and contract.kind == name
        assert contract.schema["type"] == "object"


def test_custom_names_are_idempotent_conflicting_and_creator_scoped(store):
    first = resolve(store, CUSTOM)
    assert resolve(store, CUSTOM) == first  # identical registration
    reused = resolve(store, {"$theater": "custom", "name": "verdict"})
    assert reused == first  # omitted schema reuses the registered one

    with pytest.raises(BadRequest, match="different schema"):
        resolve(store, {**CUSTOM, "schema": {"enum": ["maybe"]}})
    with pytest.raises(BadRequest, match="no custom response contract"):
        resolve(store, {"$theater": "custom", "name": "verdict"}, creator="someone-else")

    store.delete_response_contracts("parent")  # the creator's lifetime ended
    with pytest.raises(BadRequest, match="no custom response contract"):
        resolve(store, {"$theater": "custom", "name": "verdict"})


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ({"$theater": "nope"}, "unknown response contract"),
        ({"$theater": "review", "schema": {}}, "takes no other keys"),
        ({"$theater": "custom", "name": "bad name", "schema": {}}, "needs a 'name'"),
        ({"$theater": "custom", "name": "x", "schema": {"pattern": "a+"}}, "may not use"),
        ({"$theater": "custom", "name": "x", "schema": {"uniqueItems": True}}, "may not use"),
        (
            {"$theater": "custom", "name": "x", "schema": {"$ref": "https://evil.test/s"}},
            "unresolvable \\$ref",
        ),
        ({"$theater": "custom", "name": "x", "schema": {"$ref": "#"}}, "recurses"),
        ({"$theater": "custom", "name": "x", "schema": {"type": "nonsense"}}, "not valid"),
        (
            {"$theater": "custom", "name": "x", "schema": {"enum": ["x" * 40_000]}},
            "may not exceed",
        ),
    ],
)
def test_unsafe_or_malformed_strict_forms_are_refused_at_admission(store, raw, message):
    with pytest.raises(BadRequest, match=message):
        resolve(store, raw)


def test_a_property_named_like_a_forbidden_keyword_is_not_a_keyword(store):
    schema = {"type": "object", "properties": {"pattern": {"type": "string"}}}
    assert resolve(store, {"$theater": "custom", "name": "ok", "schema": schema})


# ---- whole-answer validation ------------------------------------------------------


@pytest.mark.parametrize(
    ("answer", "ok", "reason"),
    [
        (DELEGATE, True, ""),
        ("```json\n" + DELEGATE + "\n```", False, "not_json"),
        (DELEGATE + "\nDone.", False, "not_json"),
        ('{"commit": NaN}', False, "not_json"),
        ('{"commit": "x"}', False, "schema"),
        ("", False, "empty"),
        (None, False, "empty"),
        (" " * 70_000, False, "empty"),
        ('"' + "x" * 70_000 + '"', False, "too_large"),
        ('"' + "x" * 16_400 + '"', False, "too_large"),  # over the preset cap, under the custom one
    ],
)
def test_the_entire_answer_must_be_one_valid_value(store, answer, ok, reason):
    contract = parse_stored(resolve(store, {"$theater": "delegate"}))
    verdict = contract.validate(answer)
    assert (verdict.ok, verdict.reason) == (ok, reason)
    if reason == "schema":
        assert "status" in verdict.detail
    if reason == "too_large":
        assert "shorten" in verdict.detail


@pytest.mark.parametrize("preset", CONTRACT_PRESET_NAMES)
def test_presets_accept_extras_and_reject_a_missing_required_field(store, preset):
    contract = parse_stored(resolve(store, {"$theater": preset}))
    minimal = MINIMAL[preset]
    assert contract.validate(json.dumps(minimal)).ok

    field, item = NESTED[preset]
    extras = {**minimal, "task_specific": {"k": 1}, field: [{**item, "extra": True}]}
    assert contract.validate(json.dumps(extras)).ok

    for required in contract.schema["required"]:
        without = {k: v for k, v in minimal.items() if k != required}
        assert contract.validate(json.dumps(without)).reason == "schema", required


def test_custom_contracts_keep_the_larger_answer_cap(store):
    custom = parse_stored(resolve(store, {**CUSTOM, "schema": {"type": "string"}}))
    assert custom.validate('"' + "x" * 20_000 + '"').ok
    assert custom.validate('"' + "x" * 70_000 + '"').reason == "too_large"


def test_strict_guidance_follows_the_task_so_prompt_needles_differ(store):
    strict = resolve(store, {"$theater": "delegate"})
    first = _prompt_with_response_format("Audit module A for races in dispatch", strict)
    second = _prompt_with_response_format("Audit module B for leaks in dispatch", strict)

    def needle(prompt):
        return " ".join(prompt.split())[:PROMPT_MATCH]

    assert first.startswith("Audit module A") and needle(first) != needle(second)
    assert answers_prompt([first], first) and not answers_prompt([second], first)
    # A bare hint keeps today's prepend.
    assert _prompt_with_response_format("task", '{"type":"object"}').endswith("\n\ntask")


# ---- completion: JobManager is the choke point --------------------------------------


def _job(store, jobs, handle, response_format):
    return jobs.create(
        handle=handle,
        caller_id="parent",
        target_id="p1",
        kind="send",
        prompt="work",
        response_format=response_format,
    )


def test_finish_parses_valid_and_fails_closed_on_invalid_with_an_internal_audit(store):
    jobs = JobManager(store)
    strict = resolve(store, {"$theater": "delegate"})
    _job(store, jobs, "p1#1", strict)
    _job(store, jobs, "p1#2", strict)
    _job(store, jobs, "p1#3", strict)

    good = jobs.finish("p1#1", state=JobState.DONE, result=DELEGATE)
    assert (good.structured_status, good.structured_result) == ("parsed", DELEGATE)
    assert store.contract_audit_for_job("p1#1") == []

    bad = jobs.finish("p1#2", state=JobState.DONE, result="all done, see the repo")
    assert bad.state == JobState.DONE
    assert (bad.structured_status, bad.structured_result) == (STRUCTURED_UNABLE_TO_PARSE, None)
    assert bad.result == "all done, see the repo"
    (event, detail) = store.contract_audit_for_job("p1#2")[0]
    assert event == "unsupported_route" and "not_json" in detail

    crashed = jobs.finish("p1#3", state=JobState.CRASHED, error_code="boom")
    assert crashed.structured_status == "unavailable"  # a failed turn is not a parse failure


def test_hint_only_jobs_keep_their_json_loads_behaviour(store):
    jobs = JobManager(store)
    hint = resolve(store, {"type": "object"})
    _job(store, jobs, "p1#1", hint)
    _job(store, jobs, "p1#2", hint)
    assert jobs.finish("p1#1", state=JobState.DONE, result='{"anything": 1}').structured_status == (
        "parsed"
    )
    assert jobs.finish("p1#2", state=JobState.DONE, result="not json").structured_status == (
        "unavailable"
    )


def test_a_repairing_job_is_excluded_from_completion_but_not_from_safety_queries(store):
    jobs = JobManager(store)
    _job(store, jobs, "p1#1", resolve(store, {"$theater": "delegate"}))
    assert [j.handle for j in store.completion_candidate_jobs_for_target("p1")] == ["p1#1"]

    store.create_repair_attempt(
        RepairAttempt(
            attempt_id="p1#1~repair1",
            job_handle="p1#1",
            participant_id="p1",
            attempt=1,
            state=RepairState.PENDING,
            created_at=now(),
            updated_at=now(),
        )
    )

    assert store.completion_candidate_jobs_for_target("p1") == []
    assert [j.handle for j in store.active_running_jobs_for_target("p1")] == ["p1#1"]
    assert store.repair_fence_active("p1")

    tracker = CompletionTracker(store, registry=None, jobs_fn=lambda: jobs)
    tracker.answer_turn("p1", DELEGATE)  # a later turn is never matched to the repairing job
    tracker.release_jobs("p1", "rescued", error_code="rescued")  # nor does screen rescue
    tracker.finish_identity_lost_jobs("p1", "identity lost")  # nor identity loss
    assert store.get_job("p1#1").state == JobState.RUNNING
