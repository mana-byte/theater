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

from theater.transcript_identity import (
    canonical_location,
    is_opaque_location,
    same_location,
    trusted_location_unavailable_reason,
)

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


def test_is_opaque_location_colon_suffixed_relative_path_is_accepted_false_positive():
    # ``a://b`` is a legal relative POSIX path (a directory called ``a:``
    # containing ``b``), but the scheme-detection rule treats it as opaque.
    # This is the accepted collision documented in ``_OPAQUE_SCHEME_RE``'s
    # comment — pinning it here so it is a deliberate decision, not a surprise.
    assert is_opaque_location("a://b")


# --- canonical_location -----------------------------------------------------


def test_canonical_location_returns_opaque_verbatim():
    assert canonical_location("nova://abc123") == "nova://abc123"
    assert canonical_location("opencode://ses-xyz") == "opencode://ses-xyz"


def test_canonical_location_resolves_path(tmp_path):
    f = tmp_path / "transcript.jsonl"
    f.write_text("[]")
    resolved = canonical_location(str(f))
    assert resolved == str(f.resolve())


def test_canonical_location_path_with_colon_not_treated_as_opaque(tmp_path):
    # A colon in a path segment must not trigger the opaque shortcut.
    f = tmp_path / "weird:name.jsonl"
    f.write_text("[]")
    resolved = canonical_location(str(f))
    assert resolved == str(f.resolve())


def test_canonical_location_file_scheme_is_opaque_verbatim():
    # file:// names a file but is deliberately opaque — core interprets no
    # scheme. If file-URI support is ever wanted, conversion happens in the
    # adapter, not here.
    assert canonical_location("file:///tmp/transcript.jsonl") == "file:///tmp/transcript.jsonl"


# --- same_location ----------------------------------------------------------


def test_same_location_opaque_matches_itself():
    assert same_location("nova://abc123", "nova://abc123")


def test_same_location_opaque_does_not_match_different_session():
    assert not same_location("nova://abc123", "nova://def456")


def test_same_location_opaque_does_not_match_path():
    assert not same_location("nova://abc123", "/tmp/transcript.jsonl")
    assert not same_location("/tmp/transcript.jsonl", "nova://abc123")


def test_same_location_file_scheme_does_not_match_equivalent_path():
    # file:// is deliberately opaque like any other scheme; core interprets no
    # scheme, so a file URI and the path it names are different locations.
    # This is the assertion that blocks the tempting special-case.
    assert not same_location("file:///tmp/x.jsonl", "/tmp/x.jsonl")
    assert not same_location("/tmp/x.jsonl", "file:///tmp/x.jsonl")


def test_same_location_opencode_regression_guard():
    # opencode:// must keep working exactly as before.
    assert same_location("opencode://ses-1", "opencode://ses-1")
    assert not same_location("opencode://ses-1", "opencode://ses-2")


def test_same_location_paths_resolve(tmp_path):
    a = tmp_path / "a.jsonl"
    a.write_text("[]")
    # Build the string manually so the redundant "." survives — Path normalises
    # it away during construction, which would make this a tautology.
    other = f"{tmp_path}/./a.jsonl"
    assert str(a) != other
    assert same_location(str(a), other)


def test_same_location_none_is_false():
    assert not same_location(None, "nova://abc123")
    assert not same_location(None, "/tmp/transcript.jsonl")


def test_same_location_expanduser_matches_resolved(tmp_path, monkeypatch):
    """same_location must expanduser before resolving, matching canonical_location.

    Bug: the old _same_location called Path(a).resolve() without expanduser(),
    so ~/t.jsonl resolved to <cwd>/~/t.jsonl instead of the home directory.
    canonical_location did expanduser, so the two helpers disagreed about what
    the same string meant. This test fails if same_location skips expanduser.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    f = tmp_path / "t.jsonl"
    f.write_text("[]")
    tilde_path = "~/t.jsonl"
    resolved = str(f.resolve())
    # The tilde spelling must match the resolved absolute path.
    assert same_location(tilde_path, resolved)
    assert same_location(resolved, tilde_path)


def test_canonical_location_expanduser(tmp_path, monkeypatch):
    """canonical_location must expanduser before resolving."""
    monkeypatch.setenv("HOME", str(tmp_path))
    f = tmp_path / "t.jsonl"
    f.write_text("[]")
    assert canonical_location("~/t.jsonl") == str(f.resolve())


def test_same_location_legacy_non_canonical_store_row(tmp_path):
    """A legacy non-canonical transcript_location in the store must still
    match a canonical incoming location via same_location.

    Rows persisted by an older daemon may hold a non-canonical string (with ..
    segments or un-expanded symlinks). same_location must still return True
    because it canonicalises both sides before comparing.
    """
    f = tmp_path / "a.jsonl"
    f.write_text("[]")
    canonical = str(f.resolve())
    # Simulate a legacy store row with a .. segment
    legacy = f"{tmp_path}/../{tmp_path.name}/a.jsonl"
    assert legacy != canonical
    assert same_location(legacy, canonical)
    assert same_location(canonical, legacy)


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
