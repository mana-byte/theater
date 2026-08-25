from __future__ import annotations

from theater.harness.base import TokenUsage
from theater.pricing import estimate_cost_usd, usage_cost_microcents
from theater.pricing.estimation import _ALIASES, _load


def _cost(model: str) -> int:
    return usage_cost_microcents(
        TokenUsage(model=model, input_tokens=1_000_000, output_tokens=1_000_000)
    )


def test_exact_and_provider_stripped_names_use_catalog_prices():
    assert _cost("gpt-5") == 1_125_000_000
    assert _cost("openai/gpt-5") == 1_125_000_000


def test_verified_opencode_alias_uses_its_catalog_row():
    assert _cost("openai-foundry/zai-glm-5-2") == 580_000_000


def test_provider_hint_resolves_provider_qualified_catalog_name():
    cost = estimate_cost_usd(
        "FW-GLM-5.2",
        provider="azure_ai",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_read_tokens=0,
        cache_write_tokens=0,
        reasoning_tokens=0,
    )

    assert cost is not None and cost > 0


def test_missing_cache_rates_fall_back_to_input_rate():
    cost = estimate_cost_usd(
        "gpt-5",
        input_tokens=0,
        output_tokens=0,
        cache_read_tokens=0,
        cache_write_tokens=1_000_000,
        reasoning_tokens=0,
    )

    assert cost == 1.25


def test_every_explicit_alias_targets_a_priced_catalog_row():
    prices = _load()
    for target in _ALIASES.values():
        row = prices[target]
        assert row.get("input_cost_per_token", 0) > 0
        assert row.get("output_cost_per_token", 0) > 0


def test_unknown_names_do_not_fuzzy_match_catalog_entries():
    assert _cost("opus-5") == 0
    assert _cost("gpt-5-typo") == 0
    assert (
        estimate_cost_usd(
            "gpt-5-typo",
            input_tokens=1,
            output_tokens=1,
            cache_read_tokens=0,
            cache_write_tokens=0,
            reasoning_tokens=0,
        )
        is None
    )


def test_every_catalog_name_can_be_looked_up_without_error():
    for model in _load():
        usage_cost_microcents(TokenUsage(model=model, input_tokens=1, output_tokens=1))
