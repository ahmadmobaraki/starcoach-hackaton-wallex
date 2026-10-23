from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import numpy as np

from wallex_trader.config import Settings
from wallex_trader.marketdata.candles import CandleStore
from wallex_trader.models import OrderBook


@dataclass(slots=True)
class RegimeState:
    label: str
    memo: str
    btc_ret_5m_bps: float
    btc_ret_15m_bps: float
    btc_ret_1h_bps: float
    volatility_percentile: float
    average_spread_bps: float
    btc_trend: str
    created_at: datetime
    source: str = "deterministic"

    def size_multiplier(self) -> float:
        return {
            "trend_up": 1.0,
            "range": 1.0,
            "trend_down": 0.0,
            "high_vol": 0.55,
            "chaos": 0.0,
            "illiquid": 0.0,
        }.get(self.label, 0.5)

    def fit(self, signal: str) -> float:
        table = {
            "range": {"MR": 1.0, "MOM": 0.45},
            "trend_up": {"MR": 0.45, "MOM": 1.0},
            "trend_down": {"MR": 0.0, "MOM": 0.0},
            "high_vol": {"MR": 0.55, "MOM": 0.65},
            "chaos": {"MR": 0.0, "MOM": 0.0},
            "illiquid": {"MR": 0.0, "MOM": 0.0},
        }
        return table.get(self.label, {}).get(signal, 0.0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "memo": self.memo,
            "btc_ret_5m_bps": round(self.btc_ret_5m_bps, 2),
            "btc_ret_15m_bps": round(self.btc_ret_15m_bps, 2),
            "btc_ret_1h_bps": round(self.btc_ret_1h_bps, 2),
            "volatility_percentile": round(self.volatility_percentile, 1),
            "average_spread_bps": round(self.average_spread_bps, 2),
            "btc_trend": self.btc_trend,
            "created_at": self.created_at.isoformat(),
            "source": self.source,
        }


class RegimeDetector:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def detect(
        self,
        candles: CandleStore,
        books: dict[str, OrderBook],
        now: datetime | None = None,
    ) -> RegimeState:
        current = now or datetime.now(UTC)
        btc_ret_5 = btc_ret_15 = btc_ret_60 = 0.0
        vol_percentile = 50.0
        trend = "mixed"
        arrays = candles.arrays("BTCTMN", limit=300)
        if arrays is not None:
            close = arrays[3]
            if len(close) > 60:
                btc_ret_5 = float(close[-1] / close[-6] - 1) * 10_000
                btc_ret_15 = float(close[-1] / close[-16] - 1) * 10_000
                btc_ret_60 = float(close[-1] / close[-61] - 1) * 10_000
            if len(close) > 80:
                returns = np.diff(close) / close[:-1]
                rv_now = float(np.std(returns[-30:]))
                history = [
                    float(np.std(returns[i : i + 30]))
                    for i in range(0, max(1, len(returns) - 30), 5)
                ]
                if history:
                    vol_percentile = sum(1 for value in history if value <= rv_now) / len(history) * 100
                ema_short = _ema(close, 21)
                ema_long = _ema(close, 50)
                if ema_short is not None and ema_long is not None:
                    trend = "up" if ema_short > ema_long else "down"
        spreads = [
            book.spread_bps
            for book in books.values()
            if book.bids and book.asks
        ]
        average_spread = float(np.mean(spreads)) if spreads else 10_000.0

        if average_spread > max(100.0, self.settings.max_spread_bps * 2.5):
            label = "illiquid"
        elif vol_percentile >= 98 and abs(btc_ret_15) > 150:
            label = "chaos"
        elif btc_ret_60 < -40 and trend == "down":
            label = "trend_down"
        elif btc_ret_60 > 40 and trend == "up":
            label = "trend_up"
        elif vol_percentile >= 85:
            label = "high_vol"
        else:
            label = "range"

        memo = (
            f"Regime {label}: BTC 5m {btc_ret_5:+.0f} bps, 15m {btc_ret_15:+.0f} bps, "
            f"1h {btc_ret_60:+.0f} bps, trend {trend}, volatility percentile "
            f"{vol_percentile:.0f}, average spread {average_spread:.1f} bps. "
            "Spot-only, long-only: bearish regimes mean hold TMN."
        )
        return RegimeState(
            label=label,
            memo=memo,
            btc_ret_5m_bps=btc_ret_5,
            btc_ret_15m_bps=btc_ret_15,
            btc_ret_1h_bps=btc_ret_60,
            volatility_percentile=vol_percentile,
            average_spread_bps=average_spread,
            btc_trend=trend,
            created_at=current,
        )


def _ema(values: np.ndarray, period: int) -> float | None:
    if len(values) < period:
        return None
    alpha = 2.0 / (period + 1)
    ema = float(np.mean(values[:period]))
    for value in values[period:]:
        ema = alpha * float(value) + (1 - alpha) * ema
    return ema
