"""validator_for builds and checks each schema once, safely across threads."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from jsonschema import Draft202012Validator, ValidationError

from theater.frontend.capabilities import METHOD_CATALOG
from theater.frontend.schemas import catalog


@pytest.fixture(autouse=True)
def _fresh_cache():
    catalog.schema_registry()  # warm resource loading so its check_schema calls never count
    catalog._validators.clear()
    yield
    catalog._validators.clear()


@pytest.fixture
def check_calls(monkeypatch):
    calls: list[object] = []
    real = Draft202012Validator.check_schema

    def spy(schema, *args, **kwargs):
        calls.append(schema)
        return real(schema, *args, **kwargs)

    monkeypatch.setattr(Draft202012Validator, "check_schema", staticmethod(spy))
    return calls


def test_cached_instance_still_validates_every_payload():
    schema_id = METHOD_CATALOG["frontend.health.get"].params_schema_id
    first = catalog.validator_for(schema_id)
    assert catalog.validator_for(schema_id) is first
    first.validate({})
    with pytest.raises(ValidationError):
        first.validate({"unexpected": 1})
    first.validate({})


def test_each_schema_checked_once(check_calls):
    ids = {m.params_schema_id for m in list(METHOD_CATALOG.values())[:5]}
    for _ in range(20):
        for schema_id in ids:
            catalog.validator_for(schema_id)
    assert len(check_calls) == len(ids)


def test_concurrent_calls_construct_once(monkeypatch, check_calls):
    schema_id = METHOD_CATALOG["frontend.health.get"].params_schema_id
    constructions: list[int] = []

    def counting(*args, **kwargs):
        constructions.append(1)
        if len(constructions) == 1:
            time.sleep(0.2)  # a racy cache would let other missing threads overlap here
        return Draft202012Validator(*args, **kwargs)

    monkeypatch.setattr(catalog, "Draft202012Validator", counting)
    workers = 16
    barrier = threading.Barrier(workers)

    def call(_):
        barrier.wait()
        return catalog.validator_for(schema_id)

    with ThreadPoolExecutor(workers) as pool:
        got = list(pool.map(call, range(workers)))
    assert all(v is got[0] for v in got)
    assert len(constructions) == 1
    assert len(check_calls) == 1


def test_cache_is_bounded(monkeypatch):
    monkeypatch.setattr(catalog, "_VALIDATOR_CACHE_MAX", 3)
    for i in range(10):
        catalog.validator_for(f"{catalog.SCHEMA_BASE_URI}common.json#/$defs/x{i}")
    assert len(catalog._validators) == 3
