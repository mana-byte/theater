"""Vibe meta.json usage delta projection."""

from __future__ import annotations

import json
import math
import tomllib
from typing import TYPE_CHECKING

from theater.harness.base import Event, EventKind, TokenUsage
from theater.trajectory.enums import CostProvenance

from .constants import META_FILENAME, VIBE_ACTIVE_MODEL_CONFIG_KEY

if TYPE_CHECKING:
    from pathlib import Path


def _model_entry(models: object, active: str) -> dict | None:
    active_folded = active.casefold()
    if isinstance(models, list):
        for model in models:
            if not isinstance(model, dict):
                continue
            names = (model.get("name"), model.get("alias"))
            if any(isinstance(value, str) and value.casefold() == active_folded for value in names):
                return model
    elif isinstance(models, dict):
        for key, value in models.items():
            if isinstance(key, str) and key.casefold() == active_folded and isinstance(value, dict):
                return value
    return None


def _resolve_configured_model(
    config: object, active: object
) -> tuple[str | None, str | None, dict | None]:
    if not isinstance(active, str) or not active:
        return None, None, None
    models = config.get("models") if isinstance(config, dict) else None
    matched = _model_entry(models, active)
    if matched is None:
        return active, None, None
    name = matched.get("name")
    provider = matched.get("provider")
    name = name if isinstance(name, str) and name else active
    provider = provider if isinstance(provider, str) and provider else None
    model = f"{provider}/{name}" if provider is not None else name
    return model, provider, matched


def _configured_model(active: object) -> tuple[str | None, str | None, dict | None]:
    if not isinstance(active, str) or not active:
        return None, None, None
    from .launch import _config_path

    try:
        config = tomllib.loads(_config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return active, None, None
    return _resolve_configured_model(config, active)


def _price(value: object, *, positive: bool) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    price = float(value)
    if not math.isfinite(price) or price < 0 or (positive and price == 0):
        return None
    return price


def _cost_from_prices(
    input_price: object,
    output_price: object,
    cached_price: object,
    inp: int,
    out: int,
    cached: int,
) -> float | None:
    inp_rate = _price(input_price, positive=True)
    out_rate = _price(output_price, positive=True)
    if inp_rate is None or out_rate is None:
        return None
    cache_rate = _price(cached_price, positive=False)
    if cache_rate is None:
        cache_rate = inp_rate
    return inp * inp_rate / 1_000_000 + out * out_rate / 1_000_000 + cached * cache_rate / 1_000_000


class VibeUsageMixin:
    if TYPE_CHECKING:

        @property
        def path(self) -> Path | None: ...

    def _init_usage(
        self,
        *,
        after: float | None,
        session_id: str | None,
        known_location: str | None,
    ) -> None:
        self._baseline: tuple[int, int, int] | None = None
        self._meta_fingerprint: tuple[int, int, int, int] | None = None
        self._cached_meta: dict | None = None
        # A new launch may incur a model call before the observer attaches; resume baselines totals.
        self._count_initial = after is not None and session_id is None and known_location is None

    def _clear_meta_cache(self) -> None:
        self._meta_fingerprint = None
        self._cached_meta = None

    def _reset_usage(self) -> None:
        self._baseline = None
        self._clear_meta_cache()
        self._count_initial = False

    def _read_meta(self) -> dict | None:
        path = self.path
        if path is None:
            return None
        meta_path = path.parent / META_FILENAME
        try:
            st = meta_path.stat()
        except OSError:
            return None
        fingerprint = (st.st_dev, st.st_ino, st.st_mtime_ns, st.st_size)
        if self._meta_fingerprint == fingerprint:
            return self._cached_meta
        try:
            data = json.loads(meta_path.read_text())
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        self._meta_fingerprint = fingerprint
        self._cached_meta = data
        return data

    @staticmethod
    def _counter(stats: dict, name: str) -> int | None:
        value = stats.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        return value

    def _check_usage(self) -> list[Event]:
        meta = self._read_meta()
        if meta is None:
            return []
        stats = meta.get("stats")
        if not isinstance(stats, dict):
            return []
        prompt = self._counter(stats, "session_prompt_tokens")
        completion = self._counter(stats, "session_completion_tokens")
        cached = self._counter(stats, "session_cached_tokens")
        if prompt is None or completion is None or cached is None:
            return []
        current = (prompt, completion, cached)
        if self._baseline is None:
            if self._count_initial:
                self._baseline = (0, 0, 0)
                self._count_initial = False
            else:
                self._baseline = current
                return []
        if current == self._baseline:
            return []
        old_prompt, old_completion, old_cached = self._baseline
        if prompt < old_prompt or completion < old_completion or cached < old_cached:
            self._baseline = current
            return []
        d_prompt = prompt - old_prompt
        d_completion = completion - old_completion
        d_cached = cached - old_cached
        self._baseline = current
        cache_read = min(d_cached, d_prompt)
        input_tokens = d_prompt - cache_read
        if input_tokens == 0 and d_completion == 0 and cache_read == 0:
            return []
        cost_usd = self._compute_cost(meta, stats, input_tokens, d_completion, cache_read)
        model = self._resolve_model(meta)
        key = f"vibe:{old_prompt}:{old_completion}:{old_cached}->{prompt}:{completion}:{cached}"
        usage = TokenUsage(
            model=model,
            provider=self._resolve_provider(meta),
            input_tokens=input_tokens,
            output_tokens=d_completion,
            cache_read_input_tokens=cache_read,
            cost_usd=cost_usd,
            cost_provenance=(
                CostProvenance.ESTIMATED if cost_usd is not None else CostProvenance.UNKNOWN
            ),
            idempotency_key=key,
        )
        return [Event(kind=EventKind.ASSISTANT, usage=usage)]

    def _resolve_model(self, meta: dict) -> str | None:
        config = meta.get("config")
        if not isinstance(config, dict):
            return None
        active = config.get(VIBE_ACTIVE_MODEL_CONFIG_KEY)
        if isinstance(active, str) and active:
            return _resolve_configured_model(config, active)[0]
        routed = config.get("routed_model_config")
        if isinstance(routed, dict):
            name = routed.get("name")
            if isinstance(name, str) and name:
                return name
        return None

    def _resolve_provider(self, meta: dict) -> str | None:
        config = meta.get("config")
        if not isinstance(config, dict):
            return None
        active = config.get(VIBE_ACTIVE_MODEL_CONFIG_KEY)
        if isinstance(active, str) and active:
            return _resolve_configured_model(config, active)[1]
        routed = config.get("routed_model_config")
        if isinstance(routed, dict):
            provider = routed.get("provider")
            return provider if isinstance(provider, str) and provider else None
        return None

    def _compute_cost(
        self, _meta: dict, stats: dict, inp: int, out: int, cached: int
    ) -> float | None:
        return _cost_from_prices(
            stats.get("input_price_per_million"),
            stats.get("output_price_per_million"),
            stats.get("cached_input_price_per_million"),
            inp,
            out,
            cached,
        )
