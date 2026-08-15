"""Tests for ``theater.daemon.blob.blob_sha``.

The blob hash must match git's own notion of a blob hash on a repo with no
.gitattributes filters, and must return None for a file that cannot be read
— None is how a deletion is represented in the touch index, not an error.
"""

from __future__ import annotations

import hashlib

from theater.daemon.blob import blob_sha


def test_a_known_file_hashes_correctly(tmp_path):
    """The hash matches the git blob algorithm for a known input.

    Verified against ``git hash-object`` on this repo: the algorithm is
    ``sha1(b"blob %d\\0" % len(data) + data)``, hex digest. Asserting against
    a precomputed value rather than calling ``git hash-object`` so the test
    does not depend on git being installed.
    """
    f = tmp_path / "hello.txt"
    data = b"hello world\n"
    f.write_bytes(data)

    expected = hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()
    assert blob_sha(f) == expected

    # The actual SHA-1 of "blob 12\0hello world\n" — a fixed anchor that
    # would catch an algorithmic regression even if hashlib were wrong.
    assert blob_sha(f) == "3b18e512dba79e4c8300dd08aeb37f8e728b8dad"


def test_none_for_a_missing_file(tmp_path):
    """A file that does not exist returns None, not an exception.

    None is how a deletion is represented in the touch index: a path whose
    sha_before is None did not exist when the job started, and a path whose
    sha_after is None was deleted during the job.
    """
    assert blob_sha(tmp_path / "nope.txt") is None


def test_none_for_an_unreadable_file(tmp_path):
    """A file that exists but cannot be read also returns None.

    Permission errors fall here too: the contract is 'None means absent at
    that point', and an unreadable file is functionally absent for the
    purposes of content hashing.
    """
    f = tmp_path / "unreadable.txt"
    f.write_bytes(b"data")
    f.chmod(0o000)
    try:
        assert blob_sha(f) is None
    finally:
        f.chmod(0o644)


def test_empty_file_hashes_correctly(tmp_path):
    """An empty file is a valid blob with length 0."""
    f = tmp_path / "empty.txt"
    f.write_bytes(b"")
    expected = hashlib.sha1(b"blob 0\0").hexdigest()
    assert blob_sha(f) == expected
    # The well-known SHA-1 of "blob 0\0" — git's hash for an empty file.
    assert blob_sha(f) == "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"
