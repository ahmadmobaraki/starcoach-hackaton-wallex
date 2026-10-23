from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi.templating import Jinja2Templates

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent / "static"

templates = Jinja2Templates(directory=str(TEMPLATE_DIR))


def _fmt_tmn(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    return f"{number:,.0f}"


def _fmt_pct(value: Any, digits: int = 2) -> str:
    try:
        return f"{float(value) * 100:.{digits}f}%"
    except (TypeError, ValueError):
        return "—"


def _fmt_num(value: Any, digits: int = 2) -> str:
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return "—"


def _fmt_ts(value: Any, timezone: str = "Asia/Tehran") -> str:
    if not value:
        return "—"
    try:
        if isinstance(value, datetime):
            parsed = value
        else:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        local = parsed.astimezone(ZoneInfo(timezone))
        return local.strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return str(value)[:19]


def _fmt_age(value: Any) -> str:
    if not value:
        return "—"
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        seconds = max(0, int((datetime.now(UTC) - parsed).total_seconds()))
    except (ValueError, TypeError):
        return "—"
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def _json(value: Any) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False, default=str)


def _mode_color(mode: str | None) -> str:
    return {"live": "live", "shadow": "shadow", "backtest": "backtest"}.get(
        (mode or "").lower(), "muted"
    )


templates.env.filters["tmn"] = _fmt_tmn
templates.env.filters["pct"] = _fmt_pct
templates.env.filters["num"] = _fmt_num
templates.env.filters["ts"] = _fmt_ts
templates.env.filters["age"] = _fmt_age
templates.env.filters["json"] = _json
templates.env.globals["mode_color"] = _mode_color
