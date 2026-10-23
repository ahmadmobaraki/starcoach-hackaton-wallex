from __future__ import annotations

import math
from collections import deque
from datetime import UTC, datetime, timedelta
from statistics import mean, pstdev
from typing import Any

from wallex_trader.models import Side, TradeTick


class TradeTape:
    def __init__(self, maxlen: int = 4000) -> None:
        self._trades: dict[str, deque[TradeTick]] = {}
        self.maxlen = maxlen

    def add(self, trade: TradeTick) -> None:
        tape = self._trades.setdefault(trade.symbol, deque(maxlen=self.maxlen))
        if tape and tape[-1].timestamp > trade.timestamp:
            ordered = sorted([*tape, trade], key=lambda item: item.timestamp)
            self._trades[trade.symbol] = deque(ordered[-self.maxlen :], maxlen=self.maxlen)
        else:
            tape.append(trade)

    def recent(self, symbol: str, seconds: int = 60, now: datetime | None = None) -> list[TradeTick]:
        tape = self._trades.get(symbol)
        if not tape:
            return []
        cutoff = (now or datetime.now(UTC)) - timedelta(seconds=seconds)
        return [trade for trade in reversed(tape) if trade.timestamp >= cutoff][::-1]

    def stats(self, symbol: str, seconds: int = 60, now: datetime | None = None) -> dict[str, float]:
        trades = self.recent(symbol, seconds, now)
        if not trades:
            return {
                "trade_count": 0,
                "buy_volume": 0.0,
                "sell_volume": 0.0,
                "aggressive_buy_ratio": 0.5,
                "trade_notional": 0.0,
                "large_trade_ratio": 0.0,
                "avg_trade_tmn": 0.0,
                "trade_intensity": 0.0,
            }
        buy_volume = sum(float(t.quantity) for t in trades if t.taker_side == Side.BUY)
        sell_volume = sum(float(t.quantity) for t in trades if t.taker_side == Side.SELL)
        total_volume = buy_volume + sell_volume
        notionals = [float(t.notional) for t in trades]
        avg_notional = mean(notionals)
        threshold = mean(notionals) + 2 * pstdev(notionals) if len(notionals) > 3 else math.inf
        large = sum(value for value in notionals if value >= threshold)
        return {
            "trade_count": float(len(trades)),
            "buy_volume": buy_volume,
            "sell_volume": sell_volume,
            "aggressive_buy_ratio": buy_volume / total_volume if total_volume else 0.5,
            "trade_notional": sum(notionals),
            "large_trade_ratio": large / sum(notionals) if notionals else 0.0,
            "avg_trade_tmn": avg_notional,
            "trade_intensity": len(trades) / max(1, seconds) * 60.0,
        }

    def parse_payload(self, symbol: str, payload: Any) -> list[TradeTick]:
        items: Any = payload
        if isinstance(payload, dict):
            items = (
                payload.get("message")
                or payload.get("latestTrades")
                or payload.get("trades")
                or payload.get("data")
                or []
            )
        if isinstance(items, dict):
            items = [items]
        if not isinstance(items, list):
            return []
        parsed: list[TradeTick] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                ts_raw = item.get("timestamp") or item.get("ts") or item.get("time")
                if isinstance(ts_raw, (int, float)):
                    ts = datetime.fromtimestamp(
                        ts_raw / 1000 if ts_raw > 10_000_000_000 else ts_raw, tz=UTC
                    )
                elif ts_raw:
                    ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=UTC)
                else:
                    ts = datetime.now(UTC)
                trade_symbol = str(item.get("symbol") or symbol).upper()
                trade = TradeTick(
                    symbol=trade_symbol,
                    price=__import__("decimal").Decimal(str(item.get("price", 0))),
                    quantity=__import__("decimal").Decimal(
                        str(item.get("quantity", item.get("qty", 0)))
                    ),
                    is_buy_order=bool(item.get("isBuyOrder", item.get("is_buy_order", False))),
                    timestamp=ts,
                    trade_id=str(item.get("id")) if item.get("id") is not None else None,
                )
                if trade.price > 0 and trade.quantity > 0:
                    self.add(trade)
                    parsed.append(trade)
            except (TypeError, ValueError):
                continue
        return parsed
