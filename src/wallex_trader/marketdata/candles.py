from __future__ import annotations

import bisect
from collections import deque
from datetime import UTC, datetime
from typing import Any

import numpy as np

from wallex_trader.models import Candle


class CandleStore:
    def __init__(self, maxlen: int = 1500) -> None:
        self.maxlen = maxlen
        self._candles: dict[str, deque[Candle]] = {}
        self._timestamps: dict[str, list[int]] = {}

    def seed(self, symbol: str, candles: list[Candle]) -> None:
        ordered = sorted(candles, key=lambda candle: candle.ts)
        deduped: dict[int, Candle] = {int(c.ts.timestamp()): c for c in ordered}
        values = [deduped[key] for key in sorted(deduped)][-self.maxlen :]
        self._candles[symbol] = deque(values, maxlen=self.maxlen)
        self._timestamps[symbol] = [int(candle.ts.timestamp()) for candle in values]

    def upsert(self, candle: Candle) -> None:
        series = self._candles.setdefault(candle.symbol, deque(maxlen=self.maxlen))
        timestamps = self._timestamps.setdefault(candle.symbol, [])
        ts = int(candle.ts.timestamp())
        if not timestamps:
            series.append(candle)
            timestamps.append(ts)
            return
        if ts > timestamps[-1]:
            series.append(candle)
            timestamps.append(ts)
            if len(timestamps) > len(series):
                timestamps.pop(0)
            return
        if ts == timestamps[-1]:
            series[-1] = candle
            return
        # Out-of-order candles are ignored. Inserting into the middle of a full
        # deque raises "deque already at its maximum size"; live features only
        # need the newest bars, and historical gaps are filled by seed()/history
        # sync, not by the hot WebSocket path.
        index = bisect.bisect_left(timestamps, ts)
        if index < len(timestamps) and timestamps[index] == ts:
            series[index] = candle

    def upsert_udf(self, symbol: str, payload: Any) -> list[Candle]:
        if not isinstance(payload, dict):
            return []
        timestamps = payload.get("t") or []
        opens = payload.get("o") or []
        highs = payload.get("h") or []
        lows = payload.get("l") or []
        closes = payload.get("c") or []
        volumes = payload.get("v") or []
        candles: list[Candle] = []
        for index, ts in enumerate(timestamps):
            try:
                candle = Candle(
                    symbol=symbol,
                    ts=datetime.fromtimestamp(int(ts), tz=UTC),
                    open=float(opens[index]),
                    high=float(highs[index]),
                    low=float(lows[index]),
                    close=float(closes[index]),
                    volume=float(volumes[index]),
                )
            except (IndexError, TypeError, ValueError):
                continue
            self.upsert(candle)
            candles.append(candle)
        return candles

    def candles(self, symbol: str, limit: int | None = None) -> list[Candle]:
        series = self._candles.get(symbol)
        if not series:
            return []
        values = list(series)
        return values[-limit:] if limit else values

    def resampled(
        self,
        symbol: str,
        minutes: int = 1,
        limit: int = 400,
        *,
        now: datetime | None = None,
        drop_incomplete: bool = True,
    ) -> list[Candle]:
        """Aggregate the 1m store into N-minute bars for the intraday engine."""
        if minutes <= 1:
            return self.candles(symbol, limit)
        raw = self.candles(symbol, (limit + 2) * minutes)
        if not raw:
            return []
        bucket_seconds = minutes * 60
        current = now or datetime.now(UTC)
        buckets: dict[int, dict[str, Any]] = {}
        for candle in raw:
            start = int(candle.ts.timestamp()) // bucket_seconds * bucket_seconds
            bucket = buckets.setdefault(
                start,
                {
                    "symbol": candle.symbol,
                    "open": candle.open,
                    "high": candle.high,
                    "low": candle.low,
                    "close": candle.close,
                    "volume": candle.volume,
                },
            )
            bucket["high"] = max(float(bucket["high"]), candle.high)
            bucket["low"] = min(float(bucket["low"]), candle.low)
            bucket["close"] = candle.close
            bucket["volume"] = float(bucket["volume"]) + candle.volume
        result: list[Candle] = []
        for start in sorted(buckets):
            end = start + bucket_seconds
            if drop_incomplete and end > int(current.timestamp()):
                continue
            values = buckets[start]
            result.append(
                Candle(
                    symbol=str(values["symbol"]),
                    ts=datetime.fromtimestamp(end, tz=UTC),
                    open=float(values["open"]),
                    high=float(values["high"]),
                    low=float(values["low"]),
                    close=float(values["close"]),
                    volume=float(values["volume"]),
                )
            )
        return result[-limit:]

    def arrays(
        self,
        symbol: str,
        limit: int | None = None,
        *,
        minutes: int = 1,
        now: datetime | None = None,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray] | None:
        candles = self.resampled(symbol, minutes, limit or 400, now=now)
        if len(candles) < 3:
            return None
        open_ = np.array([c.open for c in candles], dtype=float)
        high = np.array([c.high for c in candles], dtype=float)
        low = np.array([c.low for c in candles], dtype=float)
        close = np.array([c.close for c in candles], dtype=float)
        volume = np.array([c.volume for c in candles], dtype=float)
        return open_, high, low, close, volume

    def last(self, symbol: str) -> Candle | None:
        series = self._candles.get(symbol)
        return series[-1] if series else None

    def last_price(self, symbol: str) -> float | None:
        candle = self.last(symbol)
        return candle.close if candle else None

    def age_ms(self, symbol: str) -> float:
        candle = self.last(symbol)
        if candle is None:
            return float("inf")
        return max(0.0, (datetime.now(UTC) - candle.ts).total_seconds() * 1000)

    def symbols(self) -> list[str]:
        return list(self._candles)
