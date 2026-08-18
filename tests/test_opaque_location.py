"""Opaque transcript locations are compared literally, never path-resolved.

A transcript location is usually a filesystem path, but a source adapter may
address a session by URI scheme instead (``opencode://``, a hypothetical
``nova://``, even ``file://``). Such locations are opaque tokens: core must
compare them literally and never ``expanduser``/``resolve``/``stat`` them,
because doing so fabricates an absolute path relative to the daemon's cwd and
persists that fabrication into the transcript-ownership index.

These tests pin the general rule introduced in WP1: any ``scheme://`` location
is opaque, and path-shaped locations keep their existing behaviour byte-for-byte.
"""

from __future__ import annotations

from theater.daemon.methods import _canonical_location, _same_location
from theater.transcript_identity import is_opaque_location, trusted_location_unavailable_reason

# --- is_opaque_location ------------------------------------------------------

#: The predicate is the one shared rule all three core sites defer to.


def test_is_opaque_location_recognises_rfc3986_schemes():
    assert is_opaque_location("opencode://ses-abc")
    assert is_opaque_location("nova://abc123")
    assert is_opaque_location("file:///tmp/transcript.jsonl")
    assert is_opaque_location("a+b-c.1://session")


def test_is_opaque_location_rejects_path_shaped_locations():
    assert not is_opaque_location("/tmp/transcript.jsonl")
    assert not is_opaque_location("relative/transcript.jsonl")
    assert not is_opaque_location("transcript.jsonl")
    # A path with a colon but no ``://`` is still a path — e.g. a tmpdir
    # whose name contains a colon on a system that allows it.
    assert not is_opaque_location("/tmp/weird:name.jsonl")


def test_is_opaque_location_rejects_scheme_without_double_slash():
    assert not is_opaque_location("opencode:/ses-abc")
    assert not is_opaque_location("mailto:foo@bar")
    assert not is_opaque_location("nova:abc123")


def test_is_opaque_location_rejects_scheme_starting_with_digit():
    # RFC 3986: scheme must begin with a letter.
    assert not is_opaque_location("1abc://session")


# --- _canonical_location -----------------------------------------------------


def test_canonical_location_returns_opaque_verbatim():
    assert _canonical_location("nova://abc123") == "nova://abc123"
    assert _canonical_location("opencode://ses-xyz") == "opencode://ses-xyz"


def test_canonical_location_resolves_path(tmp_path):
    f = tmp_path / "transcript.jsonl"
    f.write_text("[]")
    resolved = _canonical_location(str(f))
    assert resolved == str(f.resolve())


def test_canonical_location_path_with_colon_not_treated_as_opaque(tmp_path):
    # A colon in a path segment must not trigger the opaque shortcut.
    f = tmp_path / "weird:name.jsonl"
    f.write_text("[]")
    resolved = _canonical_location(str(f))
    assert resolved == str(f.resolve())


# --- _same_location ----------------------------------------------------------


def test_same_location_opaque_matches_itself():
    assert _same_location("nova://abc123", "nova://abc123")


def test_same_location_opaque_does_not_match_different_session():
    assert not _same_location("nova://abc123", "nova://def456")


def test_same_location_opaque_does_not_match_path():
    assert not _same_location("nova://abc123", "/tmp/transcript.jsonl")
    assert not _same_location("/tmp/transcript.jsonl", "nova://abc123")


def test_same_location_opencode_regression_guard():
    # opencode:// must keep working exactly as before.
    assert _same_location("opencode://ses-1", "opencode://ses-1")
    assert not _same_location("opencode://ses-1", "opencode://ses-2")


def test_same_location_paths_resolve(tmp_path):
    a = tmp_path / "a.jsonl"
    b = tmp_path / "a.jsonl"
    a.write_text("[]")
    # Same file via different string forms — resolve should make them equal.
    assert _same_location(str(a), str(b))


def test_same_location_none_is_false():
    assert not _same_location(None, "nova://abc123")
    assert not _same_location(None, "/tmp/transcript.jsonl")


# --- trusted_location_unavailable_reason -------------------------------------

#: Provenance must be trusted for the filesystem check to even run; we pass
#: a trusted provenance string throughout.


TRUSTED_PROVENANCE = "operator"


def test_trusted_location_opaque_skips_filesystem_check():
    # A trusted pin on an opaque location must never report "no longer exists
    # on disk" — its liveness is not represented by the filesystem.
    assert (
        trusted_location_unavailable_reason(
            location="nova://abc123",
            provenance=TRUSTED_PROVENANCE,
        )
        is None
    )
    assert (
        trusted_location_unavailable_reason(
            location="opencode://ses-xyz",
            provenance=TRUSTED_PROVENANCE,
        )
        is None
    )


def test_trusted_location_missing_path_reports_unavailable(tmp_path):
    missing = tmp_path / "does-not-exist.jsonl"
    reason = trusted_location_unavailable_reason(
        location=str(missing),
        provenance=TRUSTED_PROVENANCE,
    )
    assert reason is not None
    assert "no longer exists on disk" in reason


def test_trusted_location_existing_path_is_none(tmp_path):
    f = tmp_path / "transcript.jsonl"
    f.write_text("[]")
    assert (
        trusted_location_unavailable_reason(
            location=str(f),
            provenance=TRUSTED_PROVENANCE,
        )
        is None
    )


def test_trusted_location_path_with_colon_not_treated_as_opaque(tmp_path):
    # If a path with a colon were mistakenly treated as opaque, the filesystem
    # check would be skipped and a missing file would return None. Verify the
    # check runs and reports the file as missing.
    missing = tmp_path / "weird:name.jsonl"
    reason = trusted_location_unavailable_reason(
        location=str(missing),
        provenance=TRUSTED_PROVENANCE,
    )
    assert reason is not None
    assert "no longer exists on disk" in reason


def test_trusted_location_untrusted_provenance_skips_check():
    # Without trusted provenance the function short-circuits regardless of
    # whether the location is opaque or a missing path.
    assert (
        trusted_location_unavailable_reason(
            location="nova://abc123",
            provenance=None,
        )
        is None
    )
    assert (
        trusted_location_unavailable_reason(
            location="/nonexistent/path.jsonl",
            provenance=None,
        )
        is None
    )
