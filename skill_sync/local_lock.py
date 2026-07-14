"""Small cross-platform advisory locks for machine-local operations."""

from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Iterator


_thread_state = threading.local()


@contextmanager
def local_file_lock(path: str | Path, *, timeout: float = 10.0) -> Iterator[None]:
    """Hold an exclusive local lock, failing after ``timeout`` seconds."""

    lock_path = Path(path).expanduser().resolve(strict=False)
    held = getattr(_thread_state, "held_paths", None)
    if held is None:
        held = set()
        _thread_state.held_paths = held
    key = os.path.normcase(os.fspath(lock_path))
    if key in held:
        # Public workflows intentionally compose (for example import invokes
        # deploy migrate).  The outer acquisition still protects the complete
        # operation from other threads and processes.
        yield
        return
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock_file:
        if lock_file.tell() == 0:
            lock_file.write(b"\0")
            lock_file.flush()
        deadline = time.monotonic() + timeout
        while True:
            try:
                _try_lock(lock_file)
                break
            except (BlockingIOError, OSError) as exc:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"timed out waiting for local lock: {lock_path}") from exc
                time.sleep(0.05)
        held.add(key)
        try:
            yield
        finally:
            try:
                _unlock(lock_file)
            finally:
                held.remove(key)


def _try_lock(lock_file: BinaryIO) -> None:
    lock_file.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        return
    import fcntl

    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(lock_file: BinaryIO) -> None:
    lock_file.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
