from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path


class LiveLockError(RuntimeError):
    """Raised when another live process owns the lock."""


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def acquire_live_lock(path: Path, run_id: str) -> Path:
    """Create an exclusive live-run lock, reclaiming stale locks.

    The lock is advisory but fail-closed: if the recorded PID is alive, a second
    live process refuses to start instead of trading alongside the first.
    """
    lock_path = Path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if lock_path.exists():
        data: dict[str, object] = {}
        try:
            loaded = json.loads(lock_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, ValueError):
            data = {}
        try:
            pid = int(str(data.get("pid", 0) or 0))
        except (TypeError, ValueError):
            pid = 0
        if pid and pid != os.getpid() and _pid_alive(pid):
            raise LiveLockError(
                "another live process is running "
                f"(pid {pid}, run {data.get('run_id', 'unknown')}); stop it first"
            )
        lock_path.unlink(missing_ok=True)
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError as exc:
        raise LiveLockError("another live process is starting; stop it first") from exc
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(
            {
                "pid": os.getpid(),
                "run_id": run_id,
                "started_at": datetime.now(UTC).isoformat(),
            },
            handle,
        )
    return lock_path


def release_live_lock(path: Path | str | None) -> None:
    if not path:
        return
    Path(path).unlink(missing_ok=True)
