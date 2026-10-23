from __future__ import annotations

from functools import lru_cache
from pathlib import Path

BLOCKLIST_PATH = Path(__file__).resolve().parents[2] / "configs" / "blocked_markets.txt"


def normalize_market(value: str) -> str:
    return value.strip().upper().replace("/", "").replace("-", "")


def load_blocked_markets(path: Path = BLOCKLIST_PATH) -> frozenset[str]:
    symbol_path = Path(path)
    if not symbol_path.exists():
        raise FileNotFoundError(f"blocked markets file not found: {symbol_path}")
    blocked: set[str] = set()
    for raw in symbol_path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        blocked.add(normalize_market(line))
    return frozenset(blocked)


@lru_cache(maxsize=4)
def blocked_markets(path: str | Path = BLOCKLIST_PATH) -> frozenset[str]:
    return load_blocked_markets(Path(path))


def is_blocked(symbol: str, path: str | Path = BLOCKLIST_PATH) -> bool:
    return normalize_market(symbol) in blocked_markets(path)
