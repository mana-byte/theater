"""Git blob hashing without invoking git.

The `recall` feature records which files each job touched, keyed by content
hash so a later query can tell whether a file is the same one a past job left
behind. That hash has to be cheap and it has to be deterministic, and the
cheapest deterministic thing that matches git's own notion of a blob hash is
to compute it directly: `sha1(b"blob %d\\0" % len(data) + data)`.

Why not shell out to `git hash-object`? It is correct by definition, but it
forks a process per file, and a single job routinely touches dozens of paths.
Across a job's worth of paths, `git hash-object` is ~900x slower than computing
the hash in-process. That is the difference between a feature that is free to
leave on and one that has to be gated behind a flag.

The caveat: `git hash-object` applies .gitattributes filters by default, so on
a repo with CRLF conversion or an LFS clean filter the two answers diverge.
That is acceptable ONLY because we compare our hashes to our own hashes and
never to git's: a `sha_before` and a `sha_after` recorded by this function are
always computed the same way, so drift detection works regardless of what
`git hash-object` would say about the same file. Do not compare these hashes
to ones produced by `git hash-object` and conclude the index is corrupt.
"""

from __future__ import annotations

import hashlib
from pathlib import Path


def blob_sha(path: Path) -> str | None:
    """Git's blob hash for ``path``, or ``None`` if the file cannot be read.

    Computes ``sha1(b"blob %d\\0" % len(data) + data)`` — the same value
    ``git hash-object`` produces on a repo with no .gitattributes filters —
    without invoking git. See the module docstring for why that matters and
    for the filter-divergence caveat.

    ``None`` means the file was missing or unreadable. That is how a deletion
    is represented in the touch index, so it is a normal value, not an error:
    a path whose ``sha_before`` is ``None`` did not exist when the job started,
    and a path whose ``sha_after`` is ``None`` was deleted during the job.
    """
    try:
        data = path.read_bytes()
    except (OSError, ValueError):
        return None
    header = b"blob %d\0" % len(data)
    return hashlib.sha1(header + data).hexdigest()
