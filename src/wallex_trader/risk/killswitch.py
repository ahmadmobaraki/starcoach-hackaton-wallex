from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from wallex_trader.config import Settings


@dataclass(slots=True)
class KillState:
    active: bool = False
    reason: str = ""
    trigger: str = ""
    activated_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "active": self.active,
            "reason": self.reason,
            "trigger": self.trigger,
            "activated_at": self.activated_at.isoformat() if self.activated_at else None,
        }


class KillSwitch:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.state = KillState()

    @property
    def path(self) -> Path:
        return self.settings.kill_file

    def file_requested(self) -> bool:
        return self.path.exists()

    def check_file(self) -> bool:
        if self.file_requested() and not self.state.active:
            reason = ""
            try:
                reason = self.path.read_text(encoding="utf-8").strip()
            except OSError:
                reason = "kill switch file present"
            self.activate(reason or "kill switch file present", trigger="file")
            return True
        return self.state.active

    def activate(self, reason: str, *, trigger: str = "manual") -> None:
        if self.state.active:
            return
        self.state = KillState(
            active=True,
            reason=reason,
            trigger=trigger,
            activated_at=datetime.now(UTC),
        )

    def deactivate(self) -> None:
        self.state = KillState()
        try:
            if self.path.exists():
                self.path.unlink()
        except OSError:
            pass

    def write_file(self, reason: str) -> None:
        self.settings.data_dir.mkdir(parents=True, exist_ok=True)
        self.path.write_text(reason or "manual kill", encoding="utf-8")
        self.activate(reason or "manual kill", trigger="file")
