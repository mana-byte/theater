"""Runtime identity fences for declared providers and legacy tmux bindings."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.support.provider_recovery import provider_service
from theater.constants.daemon import (
    BUS_KIND_TERMINAL_RUNTIME_RESTART,
    BUS_KIND_TMUX_SERVER_RESTART,
    TERMINAL_RUNTIME_RESTART_JOB_ERROR_CODE,
    TERMINAL_RUNTIME_RESTART_TERMINATION_REASON,
    TMUX_RESTART_TERMINATION_REASON,
    terminal_runtime_identity_meta_key,
)
from theater.daemon.persistence.repositories.job_repairs import RepairAttempt, RepairState
from theater.daemon.persistence.store import Store
from theater.daemon.plugins.credentials import credential_verifier
from theater.daemon.response_contracts import resolve_response_format
from theater.daemon.terminals import TerminalProviderService
from theater.daemon.terminals.service import ProviderReportInvalid, StaleReportRevision
from theater.models import JobState, Participant, Status, TerminalBindingRecord, now


def _register(
    service: TerminalProviderService,
    name: str,
    *,
    kind: str = "synthetic",
    declared: bool = True,
) -> str:
    result = service.registry.register(
        client_id="operator",
        idempotency_key=f"register-{name}",
        params={
            "selector": name,
            "kind": kind,
            "credential_verifier": credential_verifier("credential"),
            "capabilities": [
                "terminal-provider.v1",
                *(["terminal-runtime-identity.v1"] if declared else []),
            ],
            "limits": {},
        },
    )
    return str(result["provider_id"])


def _terminal(
    provider_id: str,
    generation: int,
    participant_id: str,
    identity: str,
    *,
    legacy: bool = False,
) -> dict[str, object]:
    occupant: dict[str, object] = {"occupant_id": participant_id, "runtime_identity": identity}
    if legacy:
        occupant.update(provider_kind="tmux", tmux_server_identity=identity)
    return {
        "provider_id": provider_id,
        "provider_generation": generation,
        "terminal_id": participant_id,
        "terminal_incarnation": f"incarnation-{participant_id}",
        "occupant": occupant,
        "process": {"pid": 42},
    }


def _bind(store: Store, terminal: dict[str, object]) -> None:
    participant_id = str(terminal["terminal_id"])
    with store.write_unit() as unit:
        store.upsert_participant(
            Participant(id=participant_id, harness="codex"), connection=unit.connection
        )
        store.terminal_bindings.bind(
            TerminalBindingRecord(
                participant_id=participant_id,
                provider_id=str(terminal["provider_id"]),
                provider_generation=int(terminal["provider_generation"]),
                terminal_id=participant_id,
                terminal_incarnation=str(terminal["terminal_incarnation"]),
                occupant_evidence=dict(terminal["occupant"]),
                process_facts=dict(terminal["process"]),
                health="healthy",
                report_revision=0,
                created_at=1.0,
                updated_at=1.0,
            ),
            connection=unit.connection,
        )


def _report(
    service: TerminalProviderService,
    provider_id: str,
    generation: int,
    revision: int,
    identity: str | None,
    terminals: list[dict[str, object]],
    *,
    complete: bool = True,
    legacy: bool = False,
) -> dict:
    facts: dict[str, object] = {"terminals": terminals, "complete": complete}
    if identity is not None:
        facts["runtime_identity"] = identity
        if legacy:
            facts["tmux_server_identity"] = identity
    return service.report(provider_id, generation, revision, facts)


def test_declared_provider_reconnect_and_scoped_restart(tmp_path: Path) -> None:
    store = Store(tmp_path / "runtime-identity.db")
    try:
        service = provider_service(store)
        first_id = _register(service, "first")
        other_id = _register(service, "other")
        first_generation, _ = service.connections.acquire_callback(first_id, "credential")
        other_generation, _ = service.connections.acquire_callback(other_id, "credential")
        old = _terminal(first_id, first_generation, "old", "runtime-a")
        other = _terminal(other_id, other_generation, "other", "runtime-a")
        _bind(store, old)
        _bind(store, other)
        _report(service, first_id, first_generation, 1, "runtime-a", [old])
        _report(service, other_id, other_generation, 1, "runtime-a", [other])

        service.connections.disconnect(first_id, first_generation)
        first_generation, _ = service.connections.acquire_callback(first_id, "credential")
        old["provider_generation"] = first_generation
        _report(service, first_id, first_generation, 1, "runtime-a", [old])
        assert store.get_participant("old").status is not Status.DEAD

        fresh = _terminal(first_id, first_generation, "fresh", "runtime-b")
        _bind(store, fresh)
        finalized: list[tuple[tuple[str, ...], bool]] = []
        service.configure_runtime_restart_finalizer(
            lambda participants, legacy: finalized.append(
                (tuple(participant.id for participant in participants), legacy)
            )
        )
        _report(service, first_id, first_generation, 2, "runtime-b", [fresh])
        assert store.get_participant("old").status is Status.DEAD
        assert (
            store.get_participant("old").termination_reason
            == TERMINAL_RUNTIME_RESTART_TERMINATION_REASON
        )
        assert store.get_participant("fresh").status is not Status.DEAD
        assert store.get_participant("other").status is not Status.DEAD
        assert finalized == [(("old",), False)]
        assert (
            store.get_meta(terminal_runtime_identity_meta_key(first_id, legacy=False))
            == "runtime-b"
        )
        assert (
            store.get_meta(terminal_runtime_identity_meta_key(other_id, legacy=False))
            == "runtime-a"
        )
        incidents = [
            row for row in store.bus_tail() if row["kind"] == BUS_KIND_TERMINAL_RUNTIME_RESTART
        ]
        assert len(incidents) == 1
        assert incidents[0]["payload"]["provider_id"] == first_id
        with pytest.raises(StaleReportRevision):
            _report(service, first_id, first_generation, 2, "runtime-b", [fresh])
        assert (
            len(
                [
                    row
                    for row in store.bus_tail()
                    if row["kind"] == BUS_KIND_TERMINAL_RUNTIME_RESTART
                ]
            )
            == 1
        )
    finally:
        store.close()


def test_identity_omission_and_malformed_reports_never_retire(tmp_path: Path) -> None:
    store = Store(tmp_path / "runtime-fences.db")
    try:
        service = provider_service(store)
        provider_id = _register(service, "first")
        generation, _ = service.connections.acquire_callback(provider_id, "credential")
        old = _terminal(provider_id, generation, "old", "runtime-a")
        _bind(store, old)
        with pytest.raises(ProviderReportInvalid, match=r"requires facts\.runtime_identity"):
            _report(service, provider_id, generation, 1, None, [])
        _report(service, provider_id, generation, 1, "runtime-a", [old])
        _report(service, provider_id, generation, 2, "runtime-b", [], complete=False)
        assert store.get_participant("old").status is not Status.DEAD
        assert (
            store.get_meta(terminal_runtime_identity_meta_key(provider_id, legacy=False))
            == "runtime-a"
        )

        for bad_identity in (None, "", 17, "x" * 4097):
            with pytest.raises(ProviderReportInvalid):
                service.report(
                    provider_id,
                    generation,
                    3,
                    {"terminals": [], "complete": True, "runtime_identity": bad_identity}
                    if bad_identity is not None
                    else {"terminals": [], "complete": True},
                )
        with pytest.raises(ProviderReportInvalid, match="only tmux"):
            service.report(
                provider_id,
                generation,
                3,
                {
                    "terminals": [],
                    "complete": True,
                    "runtime_identity": "runtime-b",
                    "tmux_server_identity": "runtime-b",
                },
            )
        assert store.providers.get(provider_id).last_report_revision == 2
        assert store.get_participant("old").status is not Status.DEAD

        service.registry.update(
            client_id="operator",
            idempotency_key="drop-capability",
            params={"provider_id": provider_id, "capabilities": ["terminal-provider.v1"]},
        )
        with pytest.raises(ProviderReportInvalid, match=r"requires facts\.runtime_identity"):
            _report(service, provider_id, generation, 3, None, [])
        _report(service, provider_id, generation, 3, "runtime-b", [])
        assert store.get_participant("old").status is Status.DEAD
    finally:
        store.close()


def test_legacy_binding_provenance_survives_dual_key_report(tmp_path: Path) -> None:
    store = Store(tmp_path / "legacy-provenance.db")
    try:
        service = provider_service(store)
        provider_id = _register(service, "tmux", kind="tmux")
        generation, _ = service.connections.acquire_callback(provider_id, "credential")
        old = _terminal(provider_id, generation, "old", "runtime-a", legacy=True)
        del old["occupant"]["runtime_identity"]
        _bind(store, old)
        service.report(
            provider_id,
            generation,
            1,
            {"terminals": [], "complete": False, "tmux_server_identity": "runtime-a"},
        )
        events: list[bool] = []
        service.configure_runtime_restart_finalizer(
            lambda _participants, legacy: events.append(legacy)
        )
        _report(service, provider_id, generation, 2, "runtime-b", [], legacy=True)
        assert store.get_participant("old").termination_reason == TMUX_RESTART_TERMINATION_REASON
        assert events == [True]
        assert (
            store.get_meta(terminal_runtime_identity_meta_key(provider_id, legacy=True))
            == "runtime-b"
        )
        assert (
            store.get_meta(terminal_runtime_identity_meta_key(provider_id, legacy=False))
            == "runtime-b"
        )
        assert (
            len([row for row in store.bus_tail() if row["kind"] == BUS_KIND_TMUX_SERVER_RESTART])
            == 1
        )
    finally:
        store.close()


def test_undeclared_and_conflicting_identity_evidence_is_refused(tmp_path: Path) -> None:
    store = Store(tmp_path / "invalid-runtime-evidence.db")
    try:
        service = provider_service(store)
        plain_id = _register(service, "plain", declared=False)
        plain_generation, _ = service.connections.acquire_callback(plain_id, "credential")
        terminal = _terminal(plain_id, plain_generation, "plain-owner", "runtime-a")
        with pytest.raises(ProviderReportInvalid, match=r"facts\.runtime_identity requires"):
            _report(service, plain_id, plain_generation, 1, "runtime-a", [terminal])
        with pytest.raises(ProviderReportInvalid, match=r"occupant\.runtime_identity requires"):
            _report(service, plain_id, plain_generation, 1, None, [terminal])
        assert store.providers.get(plain_id).last_report_revision is None

        tmux_id = _register(service, "tmux", kind="tmux")
        generation, _ = service.connections.acquire_callback(tmux_id, "credential")
        occupant = _terminal(tmux_id, generation, "owner", "runtime-a", legacy=True)
        with pytest.raises(ProviderReportInvalid, match="runtime identities disagree"):
            service.report(
                tmux_id,
                generation,
                1,
                {
                    "terminals": [occupant],
                    "complete": True,
                    "runtime_identity": "runtime-a",
                    "tmux_server_identity": "runtime-b",
                },
            )
        occupant["occupant"]["tmux_server_identity"] = "runtime-b"
        with pytest.raises(ProviderReportInvalid, match="runtime identities disagree"):
            _report(service, tmux_id, generation, 1, "runtime-a", [occupant], legacy=True)
        assert store.providers.get(tmux_id).last_report_revision is None
    finally:
        store.close()


def test_restart_rolls_back_identity_and_retirement_on_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = Store(tmp_path / "runtime-rollback.db")
    try:
        service = provider_service(store)
        provider_id = _register(service, "first")
        generation, _ = service.connections.acquire_callback(provider_id, "credential")
        old = _terminal(provider_id, generation, "old", "runtime-a")
        _bind(store, old)
        _report(service, provider_id, generation, 1, "runtime-a", [old])
        finalized: list[str] = []
        service.configure_runtime_restart_finalizer(
            lambda participants, _legacy: finalized.extend(p.id for p in participants)
        )

        def fail_meta(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("meta write failed")

        monkeypatch.setattr(store._meta, "set", fail_meta)
        with pytest.raises(RuntimeError, match="meta write failed"):
            _report(service, provider_id, generation, 2, "runtime-b", [])
        assert store.get_participant("old").status is not Status.DEAD
        assert store.providers.get(provider_id).last_report_revision == 1
        assert store.get_meta(terminal_runtime_identity_meta_key(provider_id, legacy=False)) == (
            "runtime-a"
        )
        assert finalized == []
        assert not [
            row for row in store.bus_tail() if row["kind"] == BUS_KIND_TERMINAL_RUNTIME_RESTART
        ]
    finally:
        store.close()


async def test_callback_inventory_forwards_declared_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = Store(tmp_path / "runtime-inventory.db")
    try:
        service = provider_service(store)
        provider_id = _register(service, "inventory")
        generation, _ = service.connections.acquire_callback(provider_id, "credential")

        async def inventory(_provider_id: str, _generation: int, _method: str, _params: dict):
            return {
                "provider_generation": generation,
                "report_revision": 1,
                "complete": True,
                "terminals": [],
                "runtime_identity": "runtime-a",
            }

        monkeypatch.setattr(service.connections, "request", inventory)
        result = await service.inventory(provider_id, generation)
        assert result["runtime_identity"] == "runtime-a"
        assert store.get_meta(terminal_runtime_identity_meta_key(provider_id, legacy=False)) == (
            "runtime-a"
        )
    finally:
        store.close()


@pytest.mark.parametrize(
    ("kind", "declared", "fact_key", "event_kind"),
    [
        ("tmux", False, "tmux_server_identity", BUS_KIND_TMUX_SERVER_RESTART),
        ("synthetic", True, "runtime_identity", BUS_KIND_TERMINAL_RUNTIME_RESTART),
    ],
)
def test_identity_change_without_bindings_keeps_restart_event(
    tmp_path: Path, kind: str, declared: bool, fact_key: str, event_kind: str
) -> None:
    store = Store(tmp_path / "empty-runtime-change.db")
    try:
        service = provider_service(store)
        provider_id = _register(service, "empty", kind=kind, declared=declared)
        generation, _ = service.connections.acquire_callback(provider_id, "credential")
        for revision, value in enumerate(("runtime-a", "runtime-a", "runtime-b"), start=1):
            service.report(
                provider_id,
                generation,
                revision,
                {"terminals": [], "complete": True, fact_key: value},
            )
            count = len([row for row in store.bus_tail() if row["kind"] == event_kind])
            assert count == (1 if revision == 3 else 0)
        meta_key = terminal_runtime_identity_meta_key(provider_id, legacy=kind == "tmux")
        assert store.get_meta(meta_key) == "runtime-b"
        store.set_meta(meta_key, "")
        service.report(
            provider_id,
            generation,
            4,
            {"terminals": [], "complete": True, fact_key: "runtime-c"},
        )
        assert len([row for row in store.bus_tail() if row["kind"] == event_kind]) == 1
        assert store.get_meta(meta_key) == "runtime-c"
    finally:
        store.close()


def test_presence_only_report_does_not_scan_identity_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = Store(tmp_path / "presence-fast-path.db")
    try:
        service = provider_service(store)
        provider_id = _register(service, "presence")
        generation, _ = service.connections.acquire_callback(provider_id, "credential")

        def unexpected(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("presence-only report scanned identity history")

        monkeypatch.setattr(store.terminal_bindings, "list_for_provider", unexpected)
        monkeypatch.setattr(store, "get_meta", unexpected)
        service.report(provider_id, generation, 1, {"presence_invalidated": True})
    finally:
        store.close()


def test_invalid_stored_identity_retains_duty_without_authorizing_retirement(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "invalid-stored-identity.db")
    try:
        service = provider_service(store)
        provider_id = _register(service, "invalid")
        generation, _ = service.connections.acquire_callback(provider_id, "credential")
        malformed = _terminal(provider_id, generation, "old", "")
        _bind(store, malformed)
        service.registry.update(
            client_id="operator",
            idempotency_key="drop-identity-capability",
            params={"provider_id": provider_id, "capabilities": ["terminal-provider.v1"]},
        )
        service.report(provider_id, generation, 1, {"presence_invalidated": True})
        with pytest.raises(ProviderReportInvalid, match=r"requires facts\.runtime_identity"):
            _report(service, provider_id, generation, 2, None, [])
        _report(service, provider_id, generation, 2, "runtime-b", [])
        assert store.get_participant("old").status is not Status.DEAD
        assert not [
            row for row in store.bus_tail() if row["kind"] == BUS_KIND_TERMINAL_RUNTIME_RESTART
        ]

        wrong_kind = _register(service, "wrong-kind")
        wrong_generation, _ = service.connections.acquire_callback(wrong_kind, "credential")
        wrong = _terminal(wrong_kind, wrong_generation, "wrong", "runtime-a")
        del wrong["occupant"]["runtime_identity"]
        wrong["occupant"]["tmux_server_identity"] = "runtime-a"
        _bind(store, wrong)
        service.report(wrong_kind, wrong_generation, 1, {"presence_invalidated": True})
        with pytest.raises(ProviderReportInvalid, match="wrong provider kind"):
            _report(service, wrong_kind, wrong_generation, 2, "runtime-b", [wrong])
        _report(service, wrong_kind, wrong_generation, 2, "runtime-b", [])
        assert store.get_participant("wrong").status is not Status.DEAD
        assert store.providers.get(wrong_kind).last_report_revision == 2
    finally:
        store.close()


async def test_generic_restart_settles_exact_running_jobs_without_lifting_repair_fence(
    daemon,
) -> None:
    store = daemon.store
    service = daemon.terminal_service
    provider_id = _register(service, "job-settlement")
    generation, _ = service.connections.acquire_callback(provider_id, "credential")
    old = _terminal(provider_id, generation, "old", "runtime-a")
    _bind(store, old)
    store.upsert_participant(Participant(id="unaffected", harness="codex"))
    strict_format = resolve_response_format(store, {"$theater": "delegate"}, creator_id="caller")
    strict = daemon.jobs.create(
        handle="old#strict",
        caller_id="caller",
        target_id="old",
        kind="send",
        response_format=strict_format,
    )
    plain = daemon.jobs.create(handle="old#plain", caller_id="caller", target_id="old", kind="send")
    untouched = daemon.jobs.create(
        handle="unaffected#1", caller_id="caller", target_id="unaffected", kind="send"
    )
    attempt = RepairAttempt(
        attempt_id="repair-old",
        job_handle=strict.handle,
        participant_id="old",
        attempt=1,
        state=RepairState.AWAITING,
        native_turn_id="exact-repair-turn",
        turn_proven=True,
        created_at=now(),
        updated_at=now(),
    )
    assert store.create_repair_attempt(attempt)
    assert store.repair_fence_active("old")

    _report(service, provider_id, generation, 1, "runtime-a", [old])
    _report(service, provider_id, generation, 2, "runtime-b", [])

    for handle in (strict.handle, plain.handle):
        settled = store.get_job(handle)
        assert settled is not None
        assert settled.state == JobState.CRASHED
        assert settled.error_code == TERMINAL_RUNTIME_RESTART_JOB_ERROR_CODE
        assert settled.finished_at is not None
    assert store.get_job(untouched.handle).state == JobState.RUNNING
    assert store.repair_fence_active("old")
    assert store.repair_attempts_for_job(strict.handle)[0].native_turn_id == "exact-repair-turn"
