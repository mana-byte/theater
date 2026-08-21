"""Import-compatibility characterization for the theater.config package.

The refactor split theater/config.py into a package (models, validation, load,
describe) plus a theater.constants package. These tests pin that every name the
rest of the codebase imports from theater.config is still importable from
there, and that the re-exports point at the same objects the submodules define.
They do not exercise behaviour — that is test_config.py's job.
"""

from __future__ import annotations

_PUBLIC = (
    "Config",
    "ConfigError",
    "TheaterSection",
    "RailsSection",
    "ObserverSection",
    "RetentionSection",
    "HarnessSection",
    "RegieSection",
    "HARNESS_NAME",
    "MIN_INTERVAL",
    "MODELS_SECTION",
    "REASONING_SECTION",
    "load",
    "describe",
    "_SECTIONS",
)


def test_public_names_importable_from_theater_config():
    from theater import config

    missing = [n for n in _PUBLIC if not hasattr(config, n)]
    assert not missing, f"theater.config lost: {missing}"


def test_section_classes_and_sections_registry_point_at_models():
    from theater import config
    from theater.config import models

    assert config.Config is models.Config
    assert config.TheaterSection is models.TheaterSection
    assert config.RailsSection is models.RailsSection
    assert config.ObserverSection is models.ObserverSection
    assert config.RetentionSection is models.RetentionSection
    assert config.HarnessSection is models.HarnessSection
    assert config.RegieSection is models.RegieSection
    assert config._SECTIONS is models._SECTIONS
    assert config.MODELS_SECTION is models.MODELS_SECTION
    assert config.REASONING_SECTION is models.REASONING_SECTION


def test_config_error_points_at_validation():
    from theater import config
    from theater.config import validation

    assert config.ConfigError is validation.ConfigError


def test_constants_reexported_from_theater_constants():
    from theater import config, constants

    assert config.HARNESS_NAME is constants.HARNESS_NAME
    assert config.MIN_INTERVAL is constants.MIN_INTERVAL


def test_load_and_describe_are_callable_and_bind_on_the_package():
    from theater import config

    assert callable(config.load)
    assert callable(config.describe)
