"""Logging: rotation, stderr generation, token, pruning edge cases."""

from __future__ import annotations

import os

import pytest

from theater.constants.observability import STDERR_TOKEN_HEX_LEN
from theater.observability.logging import (
    create_generation_file,
    delete_generation_file,
    generate_token,
    make_formatter,
    make_rotating_handler,
    prune_stderr_generations,
    validate_token,
)


def test_token():
    t = generate_token()
    assert validate_token(t) and len(t) == STDERR_TOKEN_HEX_LEN
    assert not validate_token("xyz") and not validate_token("a" * 13)


def test_formatter():
    import logging

    fmt = make_formatter()
    rec = logging.LogRecord("t", 20, "x.py", 1, "hello", (), None)
    assert "INFO" in fmt.format(rec) and "hello" in fmt.format(rec)


def test_rotating_handler(tmp_path):
    import logging

    path = tmp_path / "daemon.log"
    h = make_rotating_handler(path, max_bytes=128, backup_count=2)
    for _ in range(10):
        h.emit(logging.LogRecord("theater", logging.INFO, "x.py", 1, "message" * 8, (), None))
    h.close()
    assert path.exists() and (tmp_path / "daemon.log.1").exists()
    assert not (tmp_path / "daemon.log.3").exists()


def test_create_gen_open_fd(tmp_path):
    path, token, fd = create_generation_file(tmp_path)
    assert path.exists() and validate_token(token)
    os.write(fd, b"test")
    os.close(fd)
    assert path.read_text() == "test"
    path.unlink()


def test_create_gen_bad_token(tmp_path):
    with pytest.raises(ValueError, match="invalid stderr token"):
        create_generation_file(tmp_path, token="nope!")


def test_delete_gen(tmp_path):
    path, _, fd = create_generation_file(tmp_path)
    os.close(fd)
    delete_generation_file(path)
    assert not path.exists()


def test_delete_rejects_non_generation(tmp_path):
    path = tmp_path / "daemon.log"
    path.write_text("keep")
    with pytest.raises(ValueError, match="not a stderr generation path"):
        delete_generation_file(path)
    assert path.exists()


@pytest.mark.parametrize(
    "current_idx,retain,deleted,survivors",
    [
        (0, 3, 2, 3),  # current oldest, 5 files
        (4, 3, 2, 3),  # current newest
        (None, 3, 2, 3),  # no current
        (2, 1, 4, 1),  # retain=1: current + 0 non-current
    ],
)
def test_prune(tmp_path, current_idx, retain, deleted, survivors):
    paths = []
    for i in range(5):
        path, _, fd = create_generation_file(tmp_path)
        os.close(fd)
        os.utime(str(path), (i, i))
        paths.append(path)
    current = paths[current_idx] if current_idx is not None else None
    assert prune_stderr_generations(tmp_path, current, retain=retain) == deleted
    assert sum(1 for p in paths if p.exists()) == survivors
    if current is not None:
        assert current.exists()
    for p in paths:
        if p.exists():
            p.unlink()


def test_prune_no_files(tmp_path):
    assert prune_stderr_generations(tmp_path, None) == 0


def test_prune_ignores_non_generation(tmp_path):
    (tmp_path / "daemon.log").write_text("not gen")
    path, _, fd = create_generation_file(tmp_path)
    os.close(fd)
    assert prune_stderr_generations(tmp_path, path) == 0
    assert (tmp_path / "daemon.log").exists()
    path.unlink()
