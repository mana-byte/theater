"""Support policies reject ambiguous JSON and noncanonical stable releases."""

from dataclasses import FrozenInstanceError

import pytest

from theater.harness.contracts.version_policy import VersionPolicy, version_key


def test_inclusive_bounds_exclusions_and_numeric_ordering():
    policy = VersionPolicy("1.2.3", "2.0.0", ("1.5.0",))
    assert all(policy.admits(version) for version in ("1.2.3", "1.2.10", "1.5.1", "2.0.0"))
    assert not any(policy.admits(version) for version in ("1.2.2", "1.5.0", "2.0.1"))
    assert version_key("12.0.103") == (12, 0, 103)
    assert policy.to_json() == {"min": "1.2.3", "max": "2.0.0", "excluded": ["1.5.0"]}
    assert VersionPolicy.from_json(policy.to_json()) == policy
    with pytest.raises(FrozenInstanceError):
        policy.minimum = "0.0.0"


@pytest.mark.parametrize(
    "version",
    [
        None,
        True,
        1,
        "",
        "1",
        "1.2",
        "1.2.3.4",
        "01.2.3",
        "1.02.3",
        "1.2.03",
        "1.2.3-beta",
        "1.2.3+build",
        "v1.2.3",
        " 1.2.3",
        "1.2.3\n",
        "١.2.3",
        "-1.2.3",
    ],
)
def test_only_canonical_stable_versions_have_keys_or_admission(version):
    with pytest.raises(ValueError, match="canonical stable"):
        version_key(version)
    assert not VersionPolicy("0.0.0", "9.9.9").admits(version)


@pytest.mark.parametrize(
    ("minimum", "maximum", "excluded"),
    [
        ("1.2.4", "1.2.3", ()),
        ("1.2", "1.2.3", ()),
        ("1.2.3", None, ()),
        ("1.2.3", "1.2.4", []),
        ("1.2.3", "1.2.4", ("1.2.3", "1.2.3")),
        ("1.2.3", "1.2.4", ("1.2.2",)),
        ("1.2.3", "1.2.4", ("1.2.3-beta",)),
        ("1.2.3", "1.2.4", (None,)),
        ("1.2.3", "1.2.4", ({},)),
    ],
)
def test_invalid_direct_policies_fail_closed(minimum, maximum, excluded):
    with pytest.raises(ValueError):
        VersionPolicy(minimum, maximum, excluded)


@pytest.mark.parametrize(
    "value",
    [
        None,
        [],
        {},
        {"min": "1.2.3", "max": "1.2.4"},
        {"min": "1.2.3", "max": "1.2.4", "excluded": [], "extra": 1},
        {"min": None, "max": "1.2.4", "excluded": []},
        {"min": "1.2.3", "max": False, "excluded": []},
        {"min": "1.2.3", "max": "1.2.4", "excluded": ()},
        {"min": "1.2.3", "max": "1.2.4", "excluded": [3]},
    ],
)
def test_json_requires_exact_keys_and_typed_values(value):
    with pytest.raises(ValueError):
        VersionPolicy.from_json(value)


@pytest.mark.parametrize(
    "text",
    [
        '{"min":"1.2.3","min":"1.2.4","max":"1.2.4","excluded":[]}',
        '{"min":"1.2.3","max":"1.2.4","excluded":[],"excluded":[]}',
        '{"min":"1.2.3","max":"1.2.4","excluded":["1.2.3","1.2.3"]}',
        '{"min":"1.2.3","max":"1.2.4","excluded":NaN}',
        '{"min":"1.2.3","max":"1.2.4","excluded":[]',
    ],
)
def test_load_rejects_ambiguous_or_invalid_json(tmp_path, text):
    path = tmp_path / "compatibility.json"
    path.write_text(text)
    with pytest.raises(ValueError):
        VersionPolicy.load(path)


def test_load_reads_a_single_release_policy(tmp_path):
    path = tmp_path / "compatibility.json"
    path.write_text('{"min":"5.5.5","max":"5.5.5","excluded":[]}\n')
    policy = VersionPolicy.load(path)
    assert policy.admits("5.5.5") and not policy.admits("5.5.6")
    assert list(policy.to_json()) == ["min", "max", "excluded"]


def test_above_max_exclusions_round_trip_and_block_a_later_range_extension():
    policy = VersionPolicy.from_text('{"min":"1.2.3","max":"1.2.4","excluded":["1.2.5"]}')
    assert not policy.admits("1.2.5")
    assert VersionPolicy.from_json(policy.to_json()) == policy
    extended = VersionPolicy(policy.minimum, "1.2.6", policy.excluded)
    assert extended.admits("1.2.6") and not extended.admits("1.2.5")
