from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from wallex_trader.runlock import LiveLockError, acquire_live_lock, release_live_lock


def test_lock_blocks_another_live_pid(tmp_path: Path) -> None:
    path = tmp_path / "live.lock"
    path.write_text(json.dumps({"pid": os.getppid(), "run_id": "other"}), encoding="utf-8")
    with pytest.raises(LiveLockError, match="another live process is running"):
        acquire_live_lock(path, "mine")


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


def test_same_process_can_reacquire(tmp_path: Path) -> None:
    path = tmp_path / "live.lock"
    acquire_live_lock(path, "one")
    acquire_live_lock(path, "two")
    assert json.loads(path.read_text(encoding="utf-8"))["run_id"] == "two"
    release_live_lock(path)
