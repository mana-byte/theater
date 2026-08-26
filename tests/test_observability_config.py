"""Config: observability section defaults, validation, describe."""

from __future__ import annotations

import pytest

from theater import config as cfg
from theater.config.models import ObservabilitySection


def test_defaults():
    s = ObservabilitySection()
    assert s.otlp_enabled is False
    assert s.agent_metrics is True
    assert s.otlp_protocol == "grpc"
    assert s.otlp_endpoint is None
    assert s.service_name == "theater"
    assert s.export_interval_ms == 5000
    assert s.gauge_interval_s == 5.0
    assert s.log_max_bytes == 10_485_760
    assert s.log_backup_count == 3


def test_in_sections_and_config():
    assert "observability" in cfg._SECTIONS
    assert cfg._SECTIONS["observability"] is ObservabilitySection
    assert isinstance(cfg.Config().observability, ObservabilitySection)


def _load(tmp_path, monkeypatch, text):
    from theater import paths

    (tmp_path / "config.toml").write_text(text)
    monkeypatch.setattr(paths, "home", lambda: tmp_path)
    return cfg.load()


INVALID = [
    ("[observability]\nbad_key = true\n", "unknown key"),
    ('[observability]\notlp_protocol = "udp"\n', "must be one of"),
    ('[observability]\notlp_endpoint = ""\n', "must not be blank"),
    ('[observability]\nservice_name = "  "\n', "must not be blank"),
    ("[observability]\nexport_interval_ms = 10\n", "must be >="),
    ("[observability]\ngauge_interval_s = 0.001\n", "must be >="),
    ("[observability]\nlog_max_bytes = 100\n", "must be >="),
    ("[observability]\nlog_backup_count = 0\n", "must be >="),
    ("[observability]\ngauge_interval_s = inf\n", "finite"),
]


@pytest.mark.parametrize("body,match", INVALID)
def test_validation(tmp_path, monkeypatch, body, match):
    with pytest.raises(cfg.ConfigError, match=match):
        _load(tmp_path, monkeypatch, body)


@pytest.mark.parametrize("proto", ["grpc", "http"])
def test_protocol(tmp_path, monkeypatch, proto):
    assert (
        _load(
            tmp_path, monkeypatch, f'[observability]\notlp_protocol = "{proto}"\n'
        ).observability.otlp_protocol
        == proto
    )


def test_otlp_enabled(tmp_path, monkeypatch):
    assert (
        _load(
            tmp_path, monkeypatch, "[observability]\notlp_enabled = true\n"
        ).observability.otlp_enabled
        is True
    )


def test_agent_metrics(tmp_path, monkeypatch):
    config = _load(tmp_path, monkeypatch, "[observability]\nagent_metrics = false\n")
    assert config.observability.agent_metrics is False
    rows = {k: (v, s) for k, v, s in cfg.describe(config)}
    assert rows["observability.agent_metrics"] == ("False", "config.toml")


def test_describe():
    rows = {k: (v, s) for k, v, s in cfg.describe(cfg.Config())}
    assert rows["observability.otlp_enabled"] == ("False", "default")
    assert rows["observability.agent_metrics"] == ("True", "default")
    assert rows["observability.otlp_protocol"] == ("grpc", "default")
    assert rows["observability.otlp_endpoint"] == ("(unset)", "default")


def test_frozen():
    with pytest.raises((AttributeError, TypeError)):
        ObservabilitySection().otlp_enabled = True  # type: ignore[misc]
