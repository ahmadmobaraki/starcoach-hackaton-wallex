from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from wallex_trader.runlock import LiveLockError, acquire_live_lock, release_live_lock


def test_lock_blocks_another_live_holder(tmp_path: Path) -> None:
    path = tmp_path / "live.lock"
    script = (
        "import fcntl, os, sys, time\n"
        "fd = os.open(sys.argv[1], os.O_CREAT | os.O_RDWR, 0o644)\n"
        "fcntl.flock(fd, fcntl.LOCK_EX)\n"
        "print('ready', flush=True)\n"
        "time.sleep(30)\n"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(path)],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "ready"
        with pytest.raises(LiveLockError, match="another live process is running"):
            acquire_live_lock(path, "mine")
    finally:
        process.terminate()
        process.wait(timeout=5)


def test_stale_lock_is_reclaimed_and_released(tmp_path: Path) -> None:
    path = tmp_path / "live.lock"
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait()
    path.write_text(json.dumps({"pid": process.pid, "run_id": "dead"}), encoding="utf-8")
    lock = acquire_live_lock(path, "mine")
    assert lock.exists()
    data = json.loads(lock.read_text(encoding="utf-8"))
    assert data["run_id"] == "mine"
    assert data["pid"] == os.getpid()
    release_live_lock(lock)
    assert not path.exists()


def test_second_acquire_is_blocked_until_release(tmp_path: Path) -> None:
    path = tmp_path / "live.lock"
    lock = acquire_live_lock(path, "one")
    with pytest.raises(LiveLockError, match="another live process is running"):
        acquire_live_lock(path, "two")
    release_live_lock(lock)
    again = acquire_live_lock(path, "three")
    assert json.loads(again.read_text(encoding="utf-8"))["run_id"] == "three"
    release_live_lock(again)
    assert not path.exists()
