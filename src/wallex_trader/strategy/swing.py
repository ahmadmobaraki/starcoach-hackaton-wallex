from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import numpy as np

from wallex_trader.config import Settings
from wallex_trader.models import Candidate, Candle, Side
from wallex_trader.strategy.regime import RegimeState


def resample_candles(
    candles: list[Candle],
    hours: int = 4,
    *,
    now: datetime | None = None,
    drop_incomplete: bool = True,
) -> list[Candle]:
    """Aggregate 1h candles into completed 4h bars labelled by close time."""
    if not candles:
        return []
    seconds = hours * 3600
    current = now or datetime.now(UTC)
    buckets: dict[int, dict[str, Any]] = {}
    for candle in candles:
        start = int(candle.ts.timestamp()) // seconds * seconds
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
        end = start + seconds
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
    return result


def _ema(values: np.ndarray, period: int) -> float | None:
    if len(values) < period:
        return None
    alpha = 2.0 / (period + 1.0)
    ema = float(np.mean(values[:period]))
    for value in values[period:]:
        ema = alpha * float(value) + (1 - alpha) * ema
    return ema


def _atr_bps(candles: list[Candle], period: int = 14) -> float | None:
    if len(candles) < period + 1:
        return None
    ranges = []
    for index in range(len(candles) - period, len(candles)):
        candle = candles[index]
        prev_close = candles[index - 1].close
        ranges.append(
            max(
                candle.high - candle.low,
                abs(candle.high - prev_close),
                abs(candle.low - prev_close),
            )
        )
    close = candles[-1].close
    if close <= 0:
        return None
    return float(np.mean(ranges)) / close * 10_000


class SwingSignalEngine:
    """Slow trend-following strategy designed for Wallex's 25-30 bps fees."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def evaluate(
        self,
        symbol: str,
        candles_4h: list[Candle],
        regime: RegimeState,
        *,
        fee_taker: float | None = None,
        fee_maker: float | None = None,
    ) -> Candidate | None:
        if len(candles_4h) < self.settings.swing_slow_ema + 2:
            return None
        closes = np.array([candle.close for candle in candles_4h], dtype=float)
        fast = _ema(closes, self.settings.swing_fast_ema)
        slow = _ema(closes, self.settings.swing_slow_ema)
        if fast is None or slow is None or slow <= 0:
            return None
        close = float(closes[-1])
        bars = self.settings.swing_momentum_bars
        if len(closes) <= bars or closes[-bars - 1] <= 0:
            return None
        ret_mom = float(closes[-1] / closes[-bars - 1] - 1)
        atr_bps = _atr_bps(candles_4h)
        if atr_bps is None:
            return None
        signal = close > fast > slow and ret_mom > 0
        if not signal:
            return None
        values = {
            "close": close,
            "ema_fast": fast,
            "ema_slow": slow,
            "ret_mom": ret_mom,
            "atr_bps": atr_bps,
            "trend_gap_bps": (close / slow - 1) * 10_000,
            "fast_gap_bps": (fast / slow - 1) * 10_000,
        }
        return self.evaluate_values(
            symbol,
            values,
            regime,
            fee_taker=fee_taker,
            fee_maker=fee_maker,
        )

    def evaluate_values(
        self,
        symbol: str,
        values: dict[str, float],
        regime: RegimeState,
        *,
        fee_taker: float | None = None,
        fee_maker: float | None = None,
    ) -> Candidate | None:
        close = float(values.get("close") or 0)
        fast = float(values.get("ema_fast") or 0)
        slow = float(values.get("ema_slow") or 0)
        ret_mom = float(values.get("ret_mom") or 0)
        atr_bps = float(values.get("atr_bps") or 0)
        if close <= 0 or fast <= 0 or slow <= 0 or atr_bps <= 0:
            return None
        if not (close > fast > slow and ret_mom > 0):
            return None
        trend_gap = max(0.0, float(values.get("trend_gap_bps") or (close / slow - 1) * 10_000))
        fast_gap = max(0.0, float(values.get("fast_gap_bps") or (fast / slow - 1) * 10_000))
        score = (
            0.55
            + 0.15 * min(1.0, trend_gap / 1000.0)
            + 0.15 * min(1.0, fast_gap / 500.0)
            + 0.15 * min(1.0, ret_mom / 0.10)
        )
        score = max(0.0, min(0.95, score))
        if score < self.settings.swing_min_score:
            return None
        spread = max(2.0, self.settings.max_spread_bps * 0.2)
        taker = fee_taker if fee_taker is not None else self.settings.taker_fee
        maker = fee_maker if fee_maker is not None else self.settings.maker_fee
        cost_bps = spread + 2 * taker * 10_000 + self.settings.cost_buffer_bps + 8.0
        expected_move = max(
            3.0 * atr_bps,
            1.5 * ret_mom * 10_000,
            300.0,
        )
        expected_move = min(expected_move, 3500.0)
        net_edge = expected_move - cost_bps
        if net_edge < self.settings.alpha_threshold_bps:
            return None
        stop_bps = max(
            self.settings.swing_stop_bps,
            1.2 * atr_bps,
        )
        stop_bps = min(max(stop_bps, self.settings.min_stop_bps), self.settings.max_stop_bps)
        take_bps = max(
            self.settings.swing_take_profit_bps,
            2.5 * stop_bps,
        )
        take_bps = min(
            max(take_bps, self.settings.min_take_profit_bps),
            self.settings.max_take_profit_bps,
        )
        features: dict[str, float | int | str | None] = {
            "price": close,
            "swing_ema_fast": fast,
            "swing_ema_slow": slow,
            "swing_ret_24h": ret_mom,
            "swing_ret_24h_bps": round(ret_mom * 10_000, 2),
            "swing_atr_bps": round(atr_bps, 2),
            "swing_trend_gap_bps": round(trend_gap, 2),
            "swing_fast_gap_bps": round(fast_gap, 2),
            "spread_bps": round(spread, 2),
            "hurdle_cost_bps": round(cost_bps, 2),
            "maker_roundtrip_bps": round(2 * maker * 10_000, 2),
            "regime": regime.label,
        }
        return Candidate(
            symbol=symbol,
            side=Side.BUY,
            signal="TREND",
            score=round(score, 4),
            expected_edge_bps=round(net_edge, 2),
            spread_bps=round(spread, 2),
            top5_depth_tmn=1_000_000_000.0,
            features=features,
            regime=regime.label,
            rationale=(
                f"slow trend: close {trend_gap:+.0f} bps above 40d EMA, "
                f"8d/40d gap {fast_gap:+.0f} bps, 24h momentum {ret_mom * 10_000:+.0f} bps, "
                f"{self.settings.swing_timeframe_hours}h ATR {atr_bps:.0f} bps; expected move {expected_move:.0f} bps "
                f"after {cost_bps:.0f} bps costs"
            ),
            expected_move_bps=round(expected_move, 2),
            recommended_stop_bps=round(stop_bps, 1),
            recommended_take_profit_bps=round(take_bps, 1),
            recommended_horizon_sec=int(self.settings.swing_horizon_sec),
        )


def swing_feature_row(
    candles_4h: list[Candle], settings: Settings
) -> dict[str, float] | None:
    """Compute the values consumed by evaluate_values from raw 4h candles."""
    if len(candles_4h) < settings.swing_slow_ema + 2:
        return None
    closes = np.array([candle.close for candle in candles_4h], dtype=float)
    fast = _ema(closes, settings.swing_fast_ema)
    slow = _ema(closes, settings.swing_slow_ema)
    atr_bps = _atr_bps(candles_4h)
    if fast is None or slow is None or atr_bps is None or len(closes) <= settings.swing_momentum_bars:
        return None
    bars = settings.swing_momentum_bars
    if closes[-bars - 1] <= 0:
        return None
    return {
        "close": float(closes[-1]),
        "ema_fast": fast,
        "ema_slow": slow,
        "ret_mom": float(closes[-1] / closes[-bars - 1] - 1),
        "atr_bps": atr_bps,
        "trend_gap_bps": (float(closes[-1]) / slow - 1) * 10_000,
        "fast_gap_bps": (fast / slow - 1) * 10_000,
    }
