from __future__ import annotations

from theater.harness.base import TokenUsage
from theater.pricing import _ALIASES, _load, usage_cost_microcents


def _cost(model: str) -> int:
    return usage_cost_microcents(
        TokenUsage(model=model, input_tokens=1_000_000, output_tokens=1_000_000)
    )


def test_exact_and_provider_stripped_names_use_catalog_prices():
    assert _cost("gpt-5") == 1_125_000_000
    assert _cost("openai/gpt-5") == 1_125_000_000


def test_verified_opencode_alias_uses_its_catalog_row():
    assert _cost("openai-foundry/zai-glm-5-2") == 580_000_000


def test_every_explicit_alias_targets_a_priced_catalog_row():
    prices = _load()
    for target in _ALIASES.values():
        row = prices[target]
        assert row.get("input_cost_per_token", 0) > 0
        assert row.get("output_cost_per_token", 0) > 0


def test_unknown_names_do_not_fuzzy_match_catalog_entries():
    assert _cost("opus-5") == 0
    assert _cost("gpt-5-typo") == 0


def test_every_catalog_name_can_be_looked_up_without_error():
    for model in _load():
        usage_cost_microcents(TokenUsage(model=model, input_tokens=1, output_tokens=1))
