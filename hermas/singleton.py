"""Fail-closed OS-level lock: one web/queue process per data directory.

This prevents a second worker from treating an active task as abandoned.
Requires a local filesystem supporting advisory locks; not a distributed lock.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def single_instance(directory: Path):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "hermas-worker.lock"
    handle = open(path, "a+b")
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            if handle.read(1) == b"":
                handle.seek(0)
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise RuntimeError("Hermas sudah berjalan dengan data directory ini") from exc
        else:
            import fcntl
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise RuntimeError("Hermas sudah berjalan dengan data directory ini") from exc
        yield
    finally:
        if os.name == "nt":
            import msvcrt
            try:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()
