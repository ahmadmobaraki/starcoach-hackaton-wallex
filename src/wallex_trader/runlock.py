from __future__ import annotations

import fcntl
import json
import os
from datetime import UTC, datetime
from pathlib import Path


class LiveLockError(RuntimeError):
    """Raised when another live process owns the lock."""


_HELD_LOCKS: dict[Path, int] = {}


def acquire_live_lock(path: Path, run_id: str) -> Path:
    """Create an exclusive live-run lock held for the life of the process.

    The advisory ``flock`` is attached to the shared data volume, so it excludes
    another live container even when both processes report PID 1. The JSON body is
    diagnostic metadata only.
    """
    lock_path = Path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(fd)
        raise LiveLockError("another live process is running; stop it first") from exc
    try:
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        with os.fdopen(os.dup(fd), "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "pid": os.getpid(),
                    "run_id": run_id,
                    "started_at": datetime.now(UTC).isoformat(),
                },
                handle,
            )
    except Exception:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
        raise
    _HELD_LOCKS[lock_path] = fd
    return lock_path


def release_live_lock(path: Path | str | None) -> None:
    if not path:
        return
    lock_path = Path(path)
    fd = _HELD_LOCKS.pop(lock_path, None)
    if fd is None:
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
    lock_path.unlink(missing_ok=True)
