from __future__ import annotations

import hashlib
import json
import math
from datetime import UTC, datetime
from typing import Any

import numpy as np

from wallex_trader.config import Settings
from wallex_trader.marketdata.candles import CandleStore
from wallex_trader.marketdata.tape import TradeTape
from wallex_trader.models import OrderBook, Side


def _finite(value: float | None, digits: int = 6) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    return round(float(value), digits)


def _ret(closes: np.ndarray, bars: int) -> float | None:
    if len(closes) <= bars or closes[-bars - 1] <= 0:
        return None
    return float(closes[-1] / closes[-bars - 1] - 1.0)


def _ema(values: np.ndarray, period: int) -> float | None:
    if len(values) < period:
        return None
    alpha = 2.0 / (period + 1.0)
    ema = float(np.mean(values[:period]))
    for value in values[period:]:
        ema = alpha * float(value) + (1 - alpha) * ema
    return ema


def _rsi(closes: np.ndarray, period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None
    deltas = np.diff(closes[-(period + 1) :])
    gains = np.clip(deltas, 0, None)
    losses = np.clip(-deltas, 0, None)
    avg_gain = float(np.mean(gains))
    avg_loss = float(np.mean(losses))
    if avg_loss <= 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def _atr(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None
    prev_close = closes[-period - 1 : -1]
    high = highs[-period:]
    low = lows[-period:]
    true_range = np.maximum(high - low, np.maximum(np.abs(high - prev_close), np.abs(low - prev_close)))
    return float(np.mean(true_range))


def _zscore(closes: np.ndarray, period: int = 20) -> float | None:
    if len(closes) < period:
        return None
    window = closes[-period:]
    std = float(np.std(window))
    if std <= 0:
        return 0.0
    return float((closes[-1] - np.mean(window)) / std)


def _realized_vol_bps(closes: np.ndarray, window: int = 30) -> float | None:
    if len(closes) < window + 1:
        return None
    returns = np.diff(closes[-(window + 1) :]) / closes[-(window + 1) : -1]
    return float(np.std(returns) * 10_000)


def _vol_percentile(closes: np.ndarray, window: int = 30, lookback: int = 240) -> float | None:
    if len(closes) < lookback + 1:
        return None
    returns = np.diff(closes[-(lookback + 1) :]) / closes[-(lookback + 1) : -1]
    current = float(np.std(returns[-window:]))
    values = [float(np.std(returns[i : i + window])) for i in range(0, len(returns) - window, 5)]
    if not values:
        return None
    return float(sum(1 for value in values if value <= current) / len(values) * 100)


def _vwap(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, volumes: np.ndarray) -> float | None:
    if len(closes) < 10:
        return None
    typical = (highs + lows + closes) / 3.0
    volume_sum = float(np.sum(volumes[-60:]))
    if volume_sum <= 0:
        return None
    return float(np.sum(typical[-60:] * volumes[-60:]) / volume_sum)


def _beta_corr(symbol_closes: np.ndarray, btc_closes: np.ndarray, window: int = 120) -> tuple[float | None, float | None]:
    length = min(len(symbol_closes), len(btc_closes), window + 1)
    if length < 20:
        return None, None
    sym = np.diff(symbol_closes[-length:]) / symbol_closes[-length:-1]
    btc = np.diff(btc_closes[-length:]) / btc_closes[-length:-1]
    if np.std(btc) <= 0:
        return None, None
    beta = float(np.cov(sym, btc)[0, 1] / np.var(btc))
    corr = float(np.corrcoef(sym, btc)[0, 1])
    return beta, corr


class FeatureEngine:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def compute(
        self,
        symbol: str,
        candles: CandleStore,
        *,
        book: OrderBook | None = None,
        tape: TradeTape | None = None,
        cross_symbols: list[str] | None = None,
        now: datetime | None = None,
        default_spread_bps: float | None = None,
        default_depth_tmn: float = 0.0,
    ) -> dict[str, Any] | None:
        arrays = candles.arrays(symbol, limit=400)
        if arrays is None:
            return None
        open_, high, low, close, volume = arrays
        if len(close) < 30:
            return None
        current = now or datetime.now(UTC)
        last = float(close[-1])
        if last <= 0:
            return None

        atr = _atr(high, low, close)
        atr_bps = (atr / last * 10_000) if atr else None
        rv_1m = _realized_vol_bps(close, 30)
        rv_5m = _realized_vol_bps(close, 5)
        rv_15 = _realized_vol_bps(close, 15)
        ret_1m = _ret(close, 1)
        ret_3m = _ret(close, 3)
        ret_5m = _ret(close, 5)
        ret_15m = _ret(close, 15)
        ret_30m = _ret(close, 30)
        ret_1h = _ret(close, 60)
        ret_4h = _ret(close, 240)

        ema9 = _ema(close, 9)
        ema21 = _ema(close, 21)
        ema50 = _ema(close, 50)
        ema50_prev = _ema(close[:-1], 50) if len(close) > 50 else None
        ema9_prev = _ema(close[:-1], 9) if len(close) > 9 else None
        slope_bps = (
            (ema9 - ema9_prev) / last * 10_000
            if ema9 is not None and ema9_prev is not None
            else None
        )
        vwap = _vwap(high, low, close, volume)

        ema50_slope_bps = (
            (ema50 - ema50_prev) / last * 10_000
            if ema50 is not None and ema50_prev is not None
            else None
        )
        vol_window = volume[-60:]
        vol_std = float(np.std(vol_window)) if len(vol_window) > 2 else 0.0
        volume_z = (
            float((volume[-1] - np.mean(vol_window)) / vol_std) if vol_std > 0 else 0.0
        )

        # Order book
        if book is not None and book.bids and book.asks:
            spread_bps = book.spread_bps
            micro = book.microprice
            mid = book.mid
            microprice_dev_bps = (
                float((micro - mid) / mid * 10_000)
                if micro is not None and mid is not None and mid > 0
                else 0.0
            )
            imbalance_1 = book.imbalance(1)
            imbalance_5 = book.imbalance(5)
            imbalance_10 = book.imbalance(10)
            top5_depth = book.top5_depth_tmn()
            depth_25 = float(
                min(
                    book.cumulative_depth(Side.BUY, 25),
                    book.cumulative_depth(Side.SELL, 25),
                )
            )
            depth_50 = float(
                min(
                    book.cumulative_depth(Side.BUY, 50),
                    book.cumulative_depth(Side.SELL, 50),
                )
            )
            depth_100 = float(
                min(
                    book.cumulative_depth(Side.BUY, 100),
                    book.cumulative_depth(Side.SELL, 100),
                )
            )
            book_age_ms = book.age_ms(current)
        else:
            spread_bps = default_spread_bps if default_spread_bps is not None else 10_000.0
            microprice_dev_bps = 0.0
            imbalance_1 = imbalance_5 = imbalance_10 = 0.0
            top5_depth = default_depth_tmn
            depth_25 = depth_50 = depth_100 = default_depth_tmn
            book_age_ms = 0.0

        flow = (
            tape.stats(symbol, 60, current)
            if tape is not None
            else {
                "trade_count": 0.0,
                "aggressive_buy_ratio": 0.5,
                "trade_notional": 0.0,
                "large_trade_ratio": 0.0,
                "avg_trade_tmn": 0.0,
                "trade_intensity": 0.0,
            }
        )

        # Cross-asset
        cross: dict[str, float | None] = {}
        btc_ret_5m = btc_ret_15m = btc_ret_1h = usdt_ret_5m = eth_ret_5m = None
        beta_btc = corr_btc = None
        btc_arrays = candles.arrays("BTCTMN", limit=240)
        if btc_arrays is not None:
            btc_close = btc_arrays[3]
            btc_ret_5m = _ret(btc_close, 5)
            btc_ret_15m = _ret(btc_close, 15)
            btc_ret_1h = _ret(btc_close, 60)
            beta_btc, corr_btc = _beta_corr(close, btc_close)
        eth_arrays = candles.arrays("ETHTMN", limit=60)
        if eth_arrays is not None:
            eth_ret_5m = _ret(eth_arrays[3], 5)
        usdt_arrays = candles.arrays("USDTTMN", limit=60)
        if usdt_arrays is not None:
            usdt_ret_5m = _ret(usdt_arrays[3], 5)
        for name in cross_symbols or ["BTCTMN", "ETHTMN", "USDTTMN"]:
            arrays_cross = candles.arrays(name, limit=60)
            cross[f"{name}_ret_5m"] = _ret(arrays_cross[3], 5) if arrays_cross is not None else None

        time_to_end_sec = None
        if self.settings.challenge_end:
            time_to_end_sec = (self.settings.challenge_end - current).total_seconds()

        features: dict[str, Any] = {
            "symbol": symbol,
            "price": _finite(last, 8),
            "ret_1m": _finite(ret_1m),
            "ret_3m": _finite(ret_3m),
            "ret_5m": _finite(ret_5m),
            "ret_15m": _finite(ret_15m),
            "ret_30m": _finite(ret_30m),
            "ret_1h": _finite(ret_1h),
            "ret_4h": _finite(ret_4h),
            "ret_5m_bps": _finite(ret_5m * 10_000 if ret_5m is not None else None, 3),
            "ret_15m_bps": _finite(ret_15m * 10_000 if ret_15m is not None else None, 3),
            "ret_1h_bps": _finite(ret_1h * 10_000 if ret_1h is not None else None, 3),
            "ret_4h_bps": _finite(ret_4h * 10_000 if ret_4h is not None else None, 3),
            "ema9": _finite(ema9, 8),
            "ema21": _finite(ema21, 8),
            "ema50": _finite(ema50, 8),
            "ema9_dist_bps": _finite((last / ema9 - 1) * 10_000 if ema9 else None, 3),
            "ema21_dist_bps": _finite((last / ema21 - 1) * 10_000 if ema21 else None, 3),
            "ema50_dist_bps": _finite((last / ema50 - 1) * 10_000 if ema50 else None, 3),
            "ema9_slope_bps": _finite(slope_bps, 3),
            "ema50_slope_bps": _finite(ema50_slope_bps, 3),
            "ema_trend": (
                "up"
                if ema9 and ema21 and ema50 and ema9 > ema21 > ema50
                else "down"
                if ema9 and ema21 and ema50 and ema9 < ema21 < ema50
                else "mixed"
            ),
            "rsi_14": _finite(_rsi(close), 2),
            "bb_z_20": _finite(_zscore(close), 3),
            "atr_bps": _finite(atr_bps, 3),
            "rv_1m_bps": _finite(rv_1m, 3),
            "rv_5m_bps": _finite(rv_5m, 3),
            "rv_15m_bps": _finite(rv_15, 3),
            "rv_percentile": _finite(_vol_percentile(close), 1),
            "price_vs_vwap_bps": _finite((last / vwap - 1) * 10_000 if vwap else None, 3),
            "volume_z_60": _finite(volume_z, 3),
            "range_compression": _finite(
                float(np.mean(high[-5:] - low[-5:]))
                / max(1e-12, float(np.mean(high[-60:] - low[-60:]))),
                3,
            ),
            "spread_bps": _finite(spread_bps, 3),
            "microprice_dev_bps": _finite(microprice_dev_bps, 3),
            "book_imbalance_1": _finite(imbalance_1, 4),
            "book_imbalance_5": _finite(imbalance_5, 4),
            "book_imbalance_10": _finite(imbalance_10, 4),
            "top5_depth_tmn": _finite(top5_depth, 2),
            "depth_25bps_tmn": _finite(depth_25, 2),
            "depth_50bps_tmn": _finite(depth_50, 2),
            "depth_100bps_tmn": _finite(depth_100, 2),
            "book_age_ms": _finite(book_age_ms, 1),
            "aggressive_buy_ratio": _finite(float(flow["aggressive_buy_ratio"]), 4),
            "trade_count_60s": _finite(float(flow["trade_count"]), 2),
            "trade_notional_60s": _finite(float(flow["trade_notional"]), 2),
            "large_trade_ratio": _finite(float(flow["large_trade_ratio"]), 4),
            "trade_intensity": _finite(float(flow["trade_intensity"]), 3),
            "btc_ret_5m": _finite(btc_ret_5m),
            "btc_ret_15m": _finite(btc_ret_15m),
            "btc_ret_1h": _finite(btc_ret_1h),
            "symbol_vs_btc_15m_bps": _finite(
                ((ret_15m or 0.0) - (btc_ret_15m or 0.0)) * 10_000, 3
            ),
            "eth_ret_5m": _finite(eth_ret_5m),
            "usdtmn_ret_5m": _finite(usdt_ret_5m),
            "beta_btc": _finite(beta_btc, 4),
            "corr_btc": _finite(corr_btc, 4),
            "hour_utc": current.hour,
            "weekday": current.weekday(),
            "time_to_end_sec": _finite(time_to_end_sec, 1),
            **{k: _finite(v) for k, v in cross.items()},
        }
        return features

    @staticmethod
    def feature_hash(features: dict[str, Any], prompt_version: str = "v1") -> str:
        payload = json.dumps(
            {"features": features, "prompt_version": prompt_version},
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(payload.encode()).hexdigest()
